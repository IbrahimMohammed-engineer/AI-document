"""
Unit tests — LLM provider infrastructure (Phase 9, Backend §34/§51).

Covers: StubLLMProvider determinism + prompt recording, retry policy
(transient-only), circuit breaker open/half-open semantics, provider
selection from settings, and process-global get/set contract.
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.infrastructure.llm import (
    CircuitOpenError,
    LLMCircuitBreaker,
    LLMChunk,
    LLMMessage,
    LLMProviderError,
    LLMUnavailableError,
    OpenAIProvider,
    StubLLMProvider,
    get_llm_provider,
    init_llm_provider,
    set_llm_provider,
)


# ── StubLLMProvider ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestStubLLMProvider:

    async def test_non_stream_returns_deterministic_response(self):
        provider = StubLLMProvider()
        messages = [LLMMessage(role="user", content="What is the process?")]
        first = await provider.generate(messages, stream=False)
        second = await provider.generate(messages, stream=False)

        assert first.content == second.content
        assert first.prompt_tokens > 0
        assert first.completion_tokens > 0
        assert "[1]" in first.content  # evidence-cited default answer

    async def test_records_last_messages(self):
        provider = StubLLMProvider()
        messages = [
            LLMMessage(role="system", content="system text"),
            LLMMessage(role="user", content="SOURCE 1 ...question"),
        ]
        await provider.generate(messages, stream=False)
        assert [m.content for m in provider.last_messages] == [
            "system text", "SOURCE 1 ...question",
        ]

    async def test_custom_responder(self):
        provider = StubLLMProvider(responder=lambda msgs: "Canned answer. [1]")
        response = await provider.generate(
            [LLMMessage(role="user", content="q")], stream=False
        )
        assert response.content == "Canned answer. [1]"

    async def test_stream_yields_words_then_usage(self):
        provider = StubLLMProvider(
            responder=lambda msgs: "one two three four"
        )
        chunks: list[LLMChunk] = [
            c async for c in provider.generate(
                [LLMMessage(role="user", content="q")], stream=True
            )
        ]
        text = "".join(c.delta for c in chunks)
        assert text == "one two three four"
        terminal = chunks[-1]
        assert terminal.finish_reason == "usage"
        assert terminal.completion_tokens == 4

    async def test_model_override_and_model_name(self):
        provider = StubLLMProvider(model="stub-llm")
        response = await provider.generate(
            [LLMMessage(role="user", content="q")], model="other-model",
            stream=False,
        )
        assert provider.model_name == "stub-llm"
        assert response.model == "other-model"


# ── Circuit breaker (Backend §51) ─────────────────────────────────────────────

@pytest.mark.unit
class TestLLMCircuitBreaker:

    def test_opens_after_threshold_consecutive_failures(self):
        breaker = LLMCircuitBreaker(threshold=3, cooldown_seconds=60.0)
        breaker.check()  # closed
        breaker.record_failure()
        breaker.record_failure()
        breaker.check()  # still closed below threshold
        breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            breaker.check()

    def test_success_resets_failure_count(self):
        breaker = LLMCircuitBreaker(threshold=3, cooldown_seconds=60.0)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        breaker.check()  # 2 < 3 after reset

    def test_cooldown_elapsed_allows_trial(self):
        breaker = LLMCircuitBreaker(threshold=1, cooldown_seconds=0.0)
        breaker.record_failure()
        # cooldown 0 → immediately half-open
        breaker.check()
        breaker.record_success()
        breaker.check()

    def test_record_failure_logs_at_threshold_once(self, caplog):
        breaker = LLMCircuitBreaker(threshold=2, cooldown_seconds=60.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open
        # repeated failures while open keep it open
        breaker.record_failure()
        assert breaker.is_open


# ── OpenAI provider resilience (retry + classification) ──────────────────────

class _FakeCompletions:
    """Fake chat.completions with a scripted list of outcomes."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class _Choice:
            class message:
                content = outcome

        class _Response:
            choices = [_Choice]
            usage = _Usage()
            model = "gpt-4o-mini"

        return _Response()


class _FakeOpenAIClient:
    def __init__(self, outcomes: list) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(outcomes)


def _openai_provider(outcomes: list, max_retries: int = 2) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test-key",
        model="gpt-4o-mini",
        max_retries=max_retries,
        breaker=LLMCircuitBreaker(threshold=100, cooldown_seconds=60.0),
        client=_FakeOpenAIClient(outcomes),
    )


@pytest.mark.unit
class TestOpenAIProviderResilience:

    async def test_transient_failure_then_success(self):
        provider = _openai_provider([RuntimeError("connection reset"), "ok"])
        response = await provider.generate(
            [LLMMessage(role="user", content="q")], stream=False
        )
        assert response.content == "ok"
        assert response.completion_tokens == 5

    async def test_retries_exhausted_raises_unavailable(self):
        provider = _openai_provider(
            [RuntimeError("boom"), RuntimeError("boom")], max_retries=2
        )
        with pytest.raises(LLMUnavailableError):
            await provider.generate(
                [LLMMessage(role="user", content="q")], stream=False
            )

    async def test_auth_error_never_retried(self):
        class _Unauthorized(Exception):
            status_code = 401

        provider = _openai_provider([_Unauthorized("bad key"), "never"], max_retries=3)
        with pytest.raises(LLMProviderError) as excinfo:
            await provider.generate(
                [LLMMessage(role="user", content="q")], stream=False
            )
        assert excinfo.value.code == "LLM_AUTH_ERROR"

    async def test_stream_establishment_failure_raises_unavailable(self):
        provider = _openai_provider([], max_retries=2)

        async def _fail_stream(**kwargs):
            raise RuntimeError("stream setup failed")

        # Replace create to fail during stream establishment
        provider._client.chat.completions.create = _fail_stream  # type: ignore[method-assign]
        with pytest.raises(LLMUnavailableError):
            async for _ in provider.generate(
                [LLMMessage(role="user", content="q")], stream=True
            ):
                pass


# ── Provider selection + process-global contract ─────────────────────────────

@pytest.mark.unit
class TestProviderSelection:

    def test_init_stub_provider(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "stub")
        get_settings.cache_clear()
        try:
            provider = init_llm_provider()
            assert isinstance(provider, StubLLMProvider)
            assert get_llm_provider() is provider
        finally:
            get_settings.cache_clear()
            set_llm_provider(None)

    def test_init_openai_requires_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        get_settings.cache_clear()
        try:
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                init_llm_provider()
        finally:
            get_settings.cache_clear()
            set_llm_provider(None)

    def test_unknown_provider_raises(self, monkeypatch):
        # Pydantic's Literal validation rejects the value at Settings parse
        # time (ValidationError subclasses ValueError) — the hard guard the
        # init function keeps is defense-in-depth for direct constructions.
        monkeypatch.setenv("LLM_PROVIDER", "widget")
        get_settings.cache_clear()
        try:
            with pytest.raises(ValueError):
                init_llm_provider()
        finally:
            get_settings.cache_clear()
            set_llm_provider(None)

    def test_get_before_init_returns_none(self):
        set_llm_provider(None)
        assert get_llm_provider() is None
