"""
LLM provider abstraction and concrete implementations (Phase 9).

Design rules (Backend §34; roadmap Phase 9 step 1) — mirrors embeddings.py /
reranker.py:
  - Abstract interface: LLMProvider.generate(messages, model, temperature,
    max_tokens, stream) — a single method covering streaming and
    non-streaming callers (streaming: async iterator of token deltas;
    non-streaming: complete response — used by the analyzer/rewriter fast
    structured-output calls, Backend §27/§28).
  - Concrete: OpenAIProvider (default), AnthropicProvider (lazy-imported SDKs).
  - Stub: StubLLMProvider — deterministic fake for tests/dev; never calls an
    API; records the last prompt it was handed (assertion hook for tests).
  - Process-global singleton managed by get/set/init_llm_provider().
  - All provider logic (retry, timeout, circuit breaking, token capture)
    lives here — nothing above the Infrastructure layer touches an SDK.

Security properties (Backend §53 item 5): the generation call is pure
text-in/text-out — NO tools/function-calling, NO code execution, no ability
to take action.  Even a successful prompt injection can only produce a
strippable sentence, never an unauthorized action.

Failure policy (Backend §51):
  - 2 attempts, exponential backoff, TRANSIENT failures only (timeouts,
    5xx, 429) — 4xx auth/validation errors are never retried.
  - Timeouts: ~30 s per generation attempt, ~5 s for fast classification
    calls (analyzer/rewriter pass ``timeout=fast``).
  - Circuit breaker: after N consecutive failures within a window the
    breaker short-circuits to immediate failure for a cooldown period.
  - Retries exhausted → LLMUnavailableError (code "LLM_UNAVAILABLE") — the
    API layer surfaces it as a user-legible, retryable SSE error event.

No I/O above this layer — callers depend on the abstract type only.

See:
  Backend-Architecture-Documentation.md §34 (LLM Integration)
  Backend-Architecture-Documentation.md §51 (External Service Failures)
  Backend-Architecture-Documentation.md §53 (Prompt Injection Protection)
  roadmap Phase 9 steps 1, 10
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)


# ── Message / response types ──────────────────────────────────────────────────

@dataclass
class LLMMessage:
    """One prompt message — provider-agnostic shape.

    ``role`` is one of "system" | "user" | "assistant".
    """

    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    """Complete (non-streaming) generation result.

    Token counts feed cost capture (Backend §34/§60) — attributed to the
    org + request by the calling stage and logged until Phase 11 persists
    them with the message row.
    """

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


@dataclass
class LLMChunk:
    """One streamed token delta.

    ``finish_reason`` is set on the terminal chunk; ``prompt_tokens`` /
    ``completion_tokens`` are populated on the terminal chunk when the
    provider reports usage (callers accumulate the text themselves).
    """

    delta: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# ── Exceptions ────────────────────────────────────────────────────────────────

class LLMProviderError(Exception):
    """Provider failure.

    ``code`` distinguishes transient failures (retryable — the class itself
    is the marker) from deterministic ones (``LLM_AUTH_ERROR``,
    ``LLM_BAD_REQUEST`` — never retried, Backend §51).
    """

    def __init__(self, message: str, *, code: str = "LLM_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    @property
    def transient(self) -> bool:
        return self.code in (
            "LLM_PROVIDER_ERROR",      # unspecified → assume transient
            "LLM_TIMEOUT",
            "LLM_RATE_LIMITED",
            "LLM_SERVER_ERROR",
        )


class LLMUnavailableError(LLMProviderError):
    """Retries exhausted / circuit breaker open — surfaced to the user as a
    retryable error state (SSE ``error`` event, Backend §51)."""

    def __init__(self, message: str = "The AI service is temporarily unavailable.") -> None:
        super().__init__(message, code="LLM_UNAVAILABLE")


class CircuitOpenError(LLMUnavailableError):
    """Circuit breaker is open — short-circuited without a provider call."""


# ── Circuit breaker (Backend §51) ─────────────────────────────────────────────

class LLMCircuitBreaker:
    """In-process consecutive-failure circuit breaker.

    After ``threshold`` consecutive failures the breaker OPENS: every call
    short-circuits to :class:`CircuitOpenError` for ``cooldown_seconds``,
    then a single trial call is allowed (half-open).  A success resets the
    failure count.  Per-process only — cross-instance coordination is a
    documented future refinement, not needed for correct degradation.
    """

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if (time.monotonic() - self._opened_at) >= self._cooldown:
            # Cooldown elapsed → half-open: allow a trial call through
            return False
        return True

    def check(self) -> None:
        """Raise CircuitOpenError when the breaker is open."""
        if self.is_open:
            raise CircuitOpenError(
                "LLM circuit breaker is open — short-circuiting provider call."
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = time.monotonic()
            logger.error(
                "LLM circuit breaker OPENED after %d consecutive failures "
                "(cooldown %.0fs)",
                self._consecutive_failures, self._cooldown,
            )

    def reset(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None


# ── Abstract base ─────────────────────────────────────────────────────────────

class LLMProvider(ABC):
    """Abstract LLM provider.

    Implementers must be safe for concurrent async use.  The abstraction
    hides SDK imports, retry/timeout policy, and token accounting from the
    rest of the application (Backend §34).
    """

    @abstractmethod
    def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        stream: bool = False,
        timeout: float | None = None,
    ) -> Awaitable[LLMResponse] | AsyncIterator[LLMChunk]:
        """Generate a completion.

        Args:
            messages:    The full prompt (system + history + user).
            model:       Model override; None = the provider's configured default.
            temperature: 0.0–0.2 for grounded answering (Backend §34).
            max_tokens:  Response cap (600–1000 for chat answers).
            stream:      True → return an async iterator of LLMChunk.
            timeout:     Per-attempt seconds; None = generation default.  Fast
                         calls (analyzer/rewriter) pass ~5 s (Backend §51).

        Returns:
            LLMResponse when ``stream=False``; AsyncIterator[LLMChunk] when
            ``stream=True`` (the terminal chunk carries usage when available).

        Raises:
            LLMUnavailableError: Retries exhausted or breaker open.
            LLMProviderError:    Non-transient provider failure (never retried).
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The canonical model identifier (logged per generation)."""


# ── Resilient base — shared retry/timeout/breaker mechanics ───────────────────

class _ResilientLLMProvider(LLMProvider):
    """Shared retry/timeout/circuit-breaker wrapper for SDK-backed providers.

    Subclasses implement :meth:`_create_once` — a single SDK call returning
    either an LLMResponse or an async iterator of provider-native chunks —
    plus :meth:`_classify_error` mapping SDK exceptions onto error codes.
    Everything else (Backend §51 policy) lives here, once.
    """

    def __init__(
        self,
        *,
        default_model: str,
        generation_timeout: float = 30.0,
        fast_timeout: float = 5.0,
        max_retries: int = 2,
        breaker: LLMCircuitBreaker | None = None,
    ) -> None:
        self._default_model = default_model
        self._generation_timeout = generation_timeout
        self._fast_timeout = fast_timeout
        self._max_retries = max_retries
        self._breaker = breaker or LLMCircuitBreaker()

    @property
    def model_name(self) -> str:
        return self._default_model

    # ── Subclass hooks ──────────────────────────────────────────────────────

    def _classify_error(self, exc: Exception) -> LLMProviderError:
        """Map an SDK exception onto a coded LLMProviderError."""
        raise NotImplementedError

    async def _create_once(
        self,
        sdk_messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> LLMResponse | AsyncIterator[Any]:
        """One raw SDK call (no retry/timeout — the caller wraps those)."""
        raise NotImplementedError

    @staticmethod
    async def _wrap_stream(
        raw_stream: AsyncIterator[Any],
        model: str,
    ) -> AsyncIterator[LLMChunk]:
        """Normalize a provider-native stream into LLMChunk deltas."""
        raise NotImplementedError

    # ── Public API ──────────────────────────────────────────────────────────

    def generate(  # type: ignore[override]
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        stream: bool = False,
        timeout: float | None = None,
    ):
        effective_model = model or self._default_model
        effective_timeout = timeout if timeout is not None else self._generation_timeout
        sdk_messages = [m.as_dict() for m in messages]

        if stream:
            return self._generate_stream(
                sdk_messages,
                model=effective_model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=effective_timeout,
            )
        return self._generate_complete(
            sdk_messages,
            model=effective_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=effective_timeout,
        )

    async def _generate_complete(
        self,
        sdk_messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> LLMResponse:
        self._breaker.check()
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            start = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self._create_once(
                        sdk_messages,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                    ),
                    timeout=timeout,
                )
                assert isinstance(response, LLMResponse)
                response.latency_ms = int((time.perf_counter() - start) * 1000)
                self._breaker.record_success()
                return response
            except LLMProviderError as exc:
                last_exc = exc
                self._on_failure(exc)
                if not exc.transient:
                    raise  # 4xx-class: retrying cannot help (Backend §51)
            except asyncio.TimeoutError:
                last_exc = LLMProviderError(
                    f"LLM call timed out after {timeout:.0f}s",
                    code="LLM_TIMEOUT",
                )
                self._on_failure(last_exc)
            except Exception as exc:  # noqa: BLE001 — SDK raises broadly
                last_exc = self._classify_error(exc)
                self._on_failure(last_exc)
                if not last_exc.transient:
                    raise last_exc from exc  # 4xx-class: never retried

            if attempt < self._max_retries:
                delay = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "LLM call failed (%s, attempt %d/%d) — retrying in %.1fs",
                    last_exc.code if isinstance(last_exc, LLMProviderError) else "unknown",
                    attempt, self._max_retries, delay,
                )
                await asyncio.sleep(delay)

        raise LLMUnavailableError(
            f"LLM unavailable after {self._max_retries} attempt(s): {last_exc}"
        ) from last_exc

    async def _generate_stream(
        self,
        sdk_messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> AsyncIterator[LLMChunk]:
        self._breaker.check()
        raw_stream: AsyncIterator[Any] | None = None
        last_exc: Exception | None = None

        # ── Retries wrap stream ESTABLISHMENT only ─────────────────────────
        for attempt in range(1, self._max_retries + 1):
            try:
                raw_stream = await asyncio.wait_for(
                    self._create_once(
                        sdk_messages,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=True,
                    ),
                    timeout=timeout,
                )
                break
            except LLMProviderError as exc:
                last_exc = exc
                self._on_failure(exc)
                if not exc.transient:
                    raise
            except asyncio.TimeoutError:
                last_exc = LLMProviderError(
                    f"LLM stream setup timed out after {timeout:.0f}s",
                    code="LLM_TIMEOUT",
                )
                self._on_failure(last_exc)
            except Exception as exc:  # noqa: BLE001
                last_exc = self._classify_error(exc)
                self._on_failure(last_exc)
                if not last_exc.transient:
                    raise last_exc from exc  # 4xx-class: never retried

            if attempt < self._max_retries:
                delay = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "LLM stream failed (%s, attempt %d/%d) — retrying in %.1fs",
                    last_exc.code if isinstance(last_exc, LLMProviderError) else "unknown",
                    attempt, self._max_retries, delay,
                )
                await asyncio.sleep(delay)

        if raw_stream is None:
            raise LLMUnavailableError(
                f"LLM stream unavailable after {self._max_retries} attempt(s): {last_exc}"
            ) from last_exc

        # ── MID-STREAM failures are NEVER retried ──────────────────────────
        # Retrying would re-generate (and re-yield) the answer from the
        # start, duplicating tokens already streamed to the client.  A
        # mid-stream provider drop propagates to the SSE ``error`` event
        # (roadmap Phase 9 §Error Handling).
        async for chunk in self._wrap_stream(raw_stream, model):
            yield chunk
        self._breaker.record_success()

    def _on_failure(self, exc: LLMProviderError) -> None:
        if exc.transient:
            self._breaker.record_failure()
        logger.warning("LLM provider failure (%s): %s", exc.code, exc.message)


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAIProvider(_ResilientLLMProvider):
    """OpenAI Chat Completions (default V1 provider).

    The client is created lazily on first use so startup never blocks on
    the SDK (mirrors the Cohere reranker pattern).  ``client`` may be
    injected directly by tests.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        *,
        generation_timeout: float = 30.0,
        fast_timeout: float = 5.0,
        max_retries: int = 2,
        breaker: LLMCircuitBreaker | None = None,
        client: Any = None,
    ) -> None:
        super().__init__(
            default_model=model,
            generation_timeout=generation_timeout,
            fast_timeout=fast_timeout,
            max_retries=max_retries,
            breaker=breaker,
        )
        self._api_key = api_key
        self._client = client  # lazy when None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise LLMProviderError(
                    "openai package is not installed.",
                    code="LLM_PROVIDER_UNAVAILABLE",
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    def _classify_error(self, exc: Exception) -> LLMProviderError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if status == 429 or "RateLimit" in name:
            return LLMProviderError(f"LLM rate limited: {exc}", code="LLM_RATE_LIMITED")
        if status is not None and 400 <= int(status) < 500:
            # Deterministic — retrying cannot help (Backend §51)
            if int(status) in (401, 403):
                return LLMProviderError(f"LLM authentication failed: {exc}", code="LLM_AUTH_ERROR")
            return LLMProviderError(f"LLM rejected the request: {exc}", code="LLM_BAD_REQUEST")
        if status is not None and int(status) >= 500:
            return LLMProviderError(f"LLM server error: {exc}", code="LLM_SERVER_ERROR")
        return LLMProviderError(f"LLM provider error: {exc}")

    async def _create_once(
        self,
        sdk_messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ):
        client = self._get_client()
        # NOTE: no tools / function-calling / response-format actions are
        # ever passed — pure text-in/text-out (Backend §53 item 5).
        if stream:
            raw = await client.chat.completions.create(
                model=model,
                messages=sdk_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            return raw
        response = await client.chat.completions.create(
            model=model,
            messages=sdk_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=(response.choices[0].message.content or ""),
            model=getattr(response, "model", model) or model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    @staticmethod
    async def _wrap_stream(raw_stream: AsyncIterator[Any], model: str) -> AsyncIterator[LLMChunk]:
        async for event in raw_stream:
            choices = getattr(event, "choices", None)
            usage = getattr(event, "usage", None)
            delta_text = ""
            finish = None
            if choices:
                choice = choices[0]
                delta_text = getattr(choice.delta, "content", None) or ""
                finish = getattr(choice, "finish_reason", None)
            elif usage is not None:
                # Terminal usage-only chunk (stream_options.include_usage)
                yield LLMChunk(
                    finish_reason="usage",
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
                continue
            if delta_text or finish:
                yield LLMChunk(delta=delta_text, finish_reason=finish)


# ── Anthropic provider ────────────────────────────────────────────────────────

class AnthropicProvider(_ResilientLLMProvider):
    """Anthropic Messages API (alternative provider — config-selected)."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-haiku-latest",
        *,
        generation_timeout: float = 30.0,
        fast_timeout: float = 5.0,
        max_retries: int = 2,
        breaker: LLMCircuitBreaker | None = None,
        client: Any = None,
    ) -> None:
        super().__init__(
            default_model=model,
            generation_timeout=generation_timeout,
            fast_timeout=fast_timeout,
            max_retries=max_retries,
            breaker=breaker,
        )
        self._api_key = api_key
        self._client = client  # lazy when None

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise LLMProviderError(
                    "anthropic package is not installed.",
                    code="LLM_PROVIDER_UNAVAILABLE",
                ) from exc
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _classify_error(self, exc: Exception) -> LLMProviderError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if status == 429 or "RateLimit" in name:
            return LLMProviderError(f"LLM rate limited: {exc}", code="LLM_RATE_LIMITED")
        if status is not None and 400 <= int(status) < 500:
            if int(status) in (401, 403):
                return LLMProviderError(f"LLM authentication failed: {exc}", code="LLM_AUTH_ERROR")
            return LLMProviderError(f"LLM rejected the request: {exc}", code="LLM_BAD_REQUEST")
        if status is not None and int(status) >= 500:
            return LLMProviderError(f"LLM server error: {exc}", code="LLM_SERVER_ERROR")
        return LLMProviderError(f"LLM provider error: {exc}")

    @staticmethod
    def _split_system(sdk_messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, str]]]:
        """Anthropic takes the system prompt as a top-level parameter."""
        system_text: str | None = None
        rest: list[dict[str, str]] = []
        for message in sdk_messages:
            if message["role"] == "system" and system_text is None:
                system_text = message["content"]
            else:
                rest.append(message)
        return system_text, rest

    async def _create_once(
        self,
        sdk_messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ):
        client = self._get_client()
        system_text, chat_messages = self._split_system(sdk_messages)
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=chat_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if system_text is not None:
            kwargs["system"] = system_text

        if stream:
            # Anthropic's SDK streams typed events; text deltas carry .text
            async def _raw_stream():
                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        yield text

            return _raw_stream()

        response = await client.messages.create(**kwargs)
        content = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            model=getattr(response, "model", model) or model,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    @staticmethod
    async def _wrap_stream(raw_stream: AsyncIterator[Any], model: str) -> AsyncIterator[LLMChunk]:
        # Anthropic streams don't report usage on the text stream; usage is
        # logged as 0 for streaming calls (Phase 11 records the model +
        # non-streamed counts where available — an accepted V1 approximation).
        async for text in raw_stream:
            if text:
                yield LLMChunk(delta=text)


# ── Stub provider (tests / dev) ───────────────────────────────────────────────

class StubLLMProvider(LLMProvider):
    """Deterministic fake LLM — never calls any API.

    - ``responder``: optional callable mapping the message list to a reply
      string (tests inject canned answers); the default produces a generic
      evidence-cited sentence.
    - Records ``last_messages`` — the assertion hook for prompt-construction
      tests (e.g. the injection fixture asserts chunk text arrives wrapped
      in SOURCE delimiters).
    - Streams word-by-word; the terminal chunk reports usage derived from a
      word-count approximation (deterministic, good enough for tests).
    """

    def __init__(
        self,
        model: str = "stub-llm",
        responder: Callable[[Sequence[LLMMessage]], str] | None = None,
    ) -> None:
        self._model = model
        self._responder = responder
        self.last_messages: list[LLMMessage] = []
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return self._model

    def _reply(self, messages: Sequence[LLMMessage]) -> str:
        self.last_messages = list(messages)
        if self._responder is not None:
            return self._responder(messages)
        return (
            "Based on the provided sources, the documents describe the "
            "requested topic. [1]"
        )

    @staticmethod
    def _count_tokens(text: str) -> int:
        return max(1, len(text.split())) if text else 0

    def generate(  # type: ignore[override]
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 800,
        stream: bool = False,
        timeout: float | None = None,
    ):
        self.call_count += 1
        if stream:
            return self._stream(messages)
        return self._complete(messages, model or self._model)

    async def _complete(
        self, messages: Sequence[LLMMessage], model: str
    ) -> LLMResponse:
        reply = self._reply(messages)
        prompt_text = "\n".join(m.content for m in messages)
        return LLMResponse(
            content=reply,
            model=model,
            prompt_tokens=self._count_tokens(prompt_text),
            completion_tokens=self._count_tokens(reply),
            latency_ms=0,
        )

    async def _stream(self, messages: Sequence[LLMMessage]) -> AsyncIterator[LLMChunk]:
        reply = self._reply(messages)
        prompt_text = "\n".join(m.content for m in messages)
        words = reply.split(" ")
        for i, word in enumerate(words):
            piece = word if i == 0 else f" {word}"
            yield LLMChunk(delta=piece, finish_reason="stop" if i == len(words) - 1 else None)
        yield LLMChunk(
            finish_reason="usage",
            prompt_tokens=self._count_tokens(prompt_text),
            completion_tokens=self._count_tokens(reply),
        )


# ── Process-global singleton ──────────────────────────────────────────────────

_provider: LLMProvider | None = None
_provider_initialized = False


def get_llm_provider() -> LLMProvider | None:
    """Return the process-global LLMProvider (None before initialization).

    Callers treat None as "LLM features unavailable" — the /ask endpoint
    responds with a typed error rather than crashing.
    """
    return _provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    """Set the process-global provider (called from lifespan/tests)."""
    global _provider, _provider_initialized
    _provider = provider
    _provider_initialized = True


def init_llm_provider() -> LLMProvider:
    """Create and register the LLM provider from app settings.

    Provider selection (Settings.llm_provider):
      - "openai":    OpenAIProvider (requires openai_api_key)
      - "anthropic": AnthropicProvider (requires anthropic_api_key)
      - "stub":      StubLLMProvider (tests/dev; no API key needed)

    Raises:
        ValueError: If the selected provider has no API key configured.
    """
    from app.core.config import get_settings

    settings = get_settings()
    provider_name = settings.llm_provider.lower()

    if provider_name == "stub":
        provider: LLMProvider = StubLLMProvider()
        logger.info("LLM provider: stub (deterministic — no API calls)")
        set_llm_provider(provider)
        return provider

    breaker = LLMCircuitBreaker(
        threshold=settings.llm_circuit_breaker_threshold,
        cooldown_seconds=settings.llm_circuit_breaker_cooldown_seconds,
    )
    shared = dict(
        generation_timeout=settings.llm_generation_timeout_seconds,
        fast_timeout=settings.llm_fast_timeout_seconds,
        max_retries=settings.llm_max_retries,
        breaker=breaker,
    )

    if provider_name == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is not configured but llm_provider='openai'. "
                "Set the key or switch llm_provider (e.g. 'anthropic')."
            )
        provider = OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            **shared,
        )
        logger.info(
            "LLM provider: OpenAI",
            extra={"model": settings.llm_model,
                   "timeout_seconds": settings.llm_generation_timeout_seconds,
                   "max_retries": settings.llm_max_retries},
        )
        set_llm_provider(provider)
        return provider

    if provider_name == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not configured but llm_provider='anthropic'. "
                "Set the key or switch llm_provider (e.g. 'openai')."
            )
        provider = AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            **shared,
        )
        logger.info(
            "LLM provider: Anthropic",
            extra={"model": settings.llm_model,
                   "timeout_seconds": settings.llm_generation_timeout_seconds,
                   "max_retries": settings.llm_max_retries},
        )
        set_llm_provider(provider)
        return provider

    raise ValueError(
        f"Unknown llm_provider='{provider_name}'. "
        f"Supported values: 'openai', 'anthropic', 'stub'."
    )
