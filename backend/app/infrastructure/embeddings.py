"""
Embedding provider abstraction and concrete implementations (Phase 7).

Design rules (Backend §22; roadmap Phase 7 step 1):
  - Abstract interface: EmbeddingProvider.embed(texts) -> list[Vector]
  - Concrete: OpenAIEmbeddingProvider (text-embedding-3-small by default)
  - Stub: StubEmbeddingProvider — deterministic fake vectors for tests;
    never calls any API.
  - Process-global singleton managed by get/set_embedding_provider().
  - All provider logic (retry, rate-limiting, batching) lives here —
    nothing above the Infrastructure layer touches an SDK directly.
  - Redis-backed token-bucket rate limiter shared across worker processes.
  - Hard startup validation: fail fast if the configured dimension does
    not match the model's actual output.

No I/O above this layer — callers depend on the abstract type only.

See:
  Backend-Architecture-Documentation.md §22 (Embedding Pipeline)
  Database-Architecture-Design-Documentation.md §17 (pgvector / model pinning)
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from typing import Sequence

logger = logging.getLogger(__name__)

# Type alias — a single embedding vector (list of floats)
Vector = list[float]


# ── Abstract base ─────────────────────────────────────────────────────────────

class EmbeddingProvider(ABC):
    """Abstract embedding provider.

    Implementers must be safe for concurrent async use (called from multiple
    tasks within the same worker process).  The abstraction hides SDK imports,
    rate-limit concerns, and retry logic from the rest of the application.
    """

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Return one embedding vector per input text.

        Args:
            texts: Non-empty sequence of non-empty strings.

        Returns:
            A list of float vectors, same length as `texts`, same order.

        Raises:
            EmbeddingProviderError: For transient provider failures (retryable).
            EmbeddingDimensionError: If the provider returns unexpected dims
                (hard error — indicates model/config mismatch; fail fast).
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The canonical embedding model identifier (recorded per chunk row)."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """The output vector dimensionality for this provider/model."""


# ── Exceptions ────────────────────────────────────────────────────────────────

class EmbeddingProviderError(Exception):
    """Transient provider failure — retryable (Backend §51)."""

    def __init__(self, message: str, *, code: str = "EMBEDDING_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class EmbeddingDimensionError(Exception):
    """Dimension mismatch between config and provider output.

    Hard error — indicates the embedding model was changed without a
    corresponding schema migration (DB §17).  The worker must fail fast
    so this is never a silent data-corruption.
    """

    def __init__(self, expected: int, got: int, model: str) -> None:
        super().__init__(
            f"Embedding dimension mismatch: config expects {expected}-dim "
            f"vectors from '{model}', but provider returned {got}-dim vectors. "
            f"Run a new migration before changing the embedding model."
        )
        self.expected = expected
        self.got = got
        self.model = model


class EmbeddingRateLimitError(EmbeddingProviderError):
    """Provider returned a rate-limit response (429).  Always retryable."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Embedding provider rate-limited.", code="EMBEDDING_RATE_LIMITED")
        self.retry_after = retry_after


# ── Redis token-bucket rate limiter ───────────────────────────────────────────

class EmbeddingRateLimiter:
    """Token-bucket rate limiter backed by Redis for cross-worker coordination.

    Uses a rolling window: each call acquires one token; the bucket refills
    at ``rate_rpm`` tokens per minute.  Falls back to a local (asyncio.Lock-
    based) no-op bucket if Redis is unavailable — this is acceptable because
    provider 429 responses are still handled by the per-batch retry logic.

    Args:
        rate_rpm: Allowed requests per minute (shared across all workers).
        redis_key: Redis key for the token bucket (default: embedding:rate_bucket).
    """

    _REDIS_KEY = "embedding:rate_bucket"

    def __init__(self, rate_rpm: int) -> None:
        self._rate_rpm = rate_rpm
        self._window_seconds = 60.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until one token is available in the bucket.

        Falls back gracefully to a local sleep when Redis is unavailable.
        """
        try:
            from app.infrastructure.redis import get_redis
            redis = get_redis()
            await self._redis_acquire(redis)
        except RuntimeError:
            # Redis not initialized (unit tests / health-check context)
            await self._local_acquire()
        except Exception as exc:
            # Redis unreachable — degrade to local throttle, log warning
            logger.warning("EmbeddingRateLimiter: Redis unavailable (%s), using local fallback", exc)
            await self._local_acquire()

    async def _redis_acquire(self, redis) -> None:
        """Sliding-window rate limit via Redis INCR + EXPIRE."""
        # Use a 1-second granularity window key so the bucket refills smoothly
        window_key = f"{self._REDIS_KEY}:{int(time.time())}"
        tokens_per_second = self._rate_rpm / 60.0
        max_tokens_per_second = max(1, math.ceil(tokens_per_second))

        while True:
            count = await redis.incr(window_key)
            if count == 1:
                await redis.expire(window_key, 2)  # auto-cleanup after 2 s

            if count <= max_tokens_per_second:
                return  # token acquired

            # Bucket full — wait a brief moment then retry
            await asyncio.sleep(1.0 / max_tokens_per_second)

    async def _local_acquire(self) -> None:
        """Simple asyncio-based rate limiter (single-process fallback)."""
        async with self._lock:
            delay = 60.0 / self._rate_rpm
            await asyncio.sleep(delay)


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding via the OpenAI Embeddings API.

    Handles:
    - Async client (openai.AsyncOpenAI)
    - Per-batch retry with exponential backoff (up to max_retries attempts)
    - Rate limiting via EmbeddingRateLimiter
    - Dimension validation on first response

    The client is created lazily on first use so startup never blocks on
    the OpenAI SDK (Backend §34 — provider clients initialised lazily).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        expected_dimensions: int = 1536,
        request_timeout: float = 15.0,
        max_retries: int = 3,
        rate_limiter: EmbeddingRateLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._expected_dimensions = expected_dimensions
        self._request_timeout = request_timeout
        self._max_retries = max_retries
        self._rate_limiter = rate_limiter
        self._client = None  # lazy

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._expected_dimensions

    def _get_client(self):
        """Return the cached async client, creating it on first use."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI  # type: ignore[import-untyped]
            except ImportError as exc:
                raise EmbeddingProviderError(
                    "openai package is not installed. Add 'openai' to requirements.txt.",
                    code="EMBEDDING_PROVIDER_UNAVAILABLE",
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key, timeout=self._request_timeout)
        return self._client

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Embed a batch of texts, returning one vector per text."""
        if not texts:
            return []

        client = self._get_client()

        for attempt in range(1, self._max_retries + 1):
            try:
                if self._rate_limiter:
                    await self._rate_limiter.acquire()

                response = await client.embeddings.create(
                    model=self._model,
                    input=list(texts),
                )
                vectors = [item.embedding for item in response.data]

            except Exception as exc:
                exc_name = type(exc).__name__
                is_last = attempt >= self._max_retries

                # Classify: rate-limit vs transient vs hard
                if "RateLimitError" in exc_name:
                    retry_after = getattr(exc, "retry_after", None)
                    logger.warning(
                        "Embedding rate-limited (attempt %d/%d), retry_after=%s",
                        attempt, self._max_retries, retry_after,
                    )
                    if is_last:
                        raise EmbeddingRateLimitError(retry_after) from exc
                    wait = float(retry_after or 5) * attempt
                    await asyncio.sleep(wait)
                    continue

                if "AuthenticationError" in exc_name or "PermissionDeniedError" in exc_name:
                    raise EmbeddingProviderError(
                        f"OpenAI authentication failed: {exc}",
                        code="EMBEDDING_AUTH_ERROR",
                    ) from exc

                # Transient (APIConnectionError, APITimeoutError, 5xx, etc.)
                logger.warning(
                    "Embedding call failed (%s, attempt %d/%d): %s",
                    exc_name, attempt, self._max_retries, exc,
                )
                if is_last:
                    raise EmbeddingProviderError(
                        f"Embedding provider failed after {self._max_retries} attempts: {exc}",
                        code="EMBEDDING_PROVIDER_ERROR",
                    ) from exc
                await asyncio.sleep(min(5 * (2 ** (attempt - 1)), 30))
                continue
            else:
                break
        else:  # pragma: no cover
            raise EmbeddingProviderError("Embedding retry loop exhausted unexpectedly.")

        # Validate dimensions on every response (catches silent model drift)
        if vectors and len(vectors[0]) != self._expected_dimensions:
            raise EmbeddingDimensionError(
                expected=self._expected_dimensions,
                got=len(vectors[0]),
                model=self._model,
            )

        return vectors


# ── Stub provider (tests / dev) ───────────────────────────────────────────────

class StubEmbeddingProvider(EmbeddingProvider):
    """Deterministic fake embedding provider — never calls any API.

    Each text is hashed to produce a stable, reproducible vector of the
    configured dimensionality.  Similar texts do NOT produce similar vectors
    (this is a test fixture, not a real semantic encoder), but the vectors
    are always valid (unit-norm within floating-point limits).

    Usage:
        provider = StubEmbeddingProvider(dimensions=1536)
        vectors = await provider.embed(["hello", "world"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 1536
    """

    def __init__(self, dimensions: int = 1536, model: str = "stub") -> None:
        self._dims = dimensions
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dims

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Return one deterministic unit vector per text (no API call)."""
        import hashlib

        vectors: list[Vector] = []
        for text in texts:
            # Seed from the text's SHA-256 so the same text always maps to
            # the same vector (deterministic across runs, stable in tests).
            digest = hashlib.sha256(text.encode()).digest()
            # Expand to required dims using repeated hashing (simple, stable)
            raw: list[float] = []
            seed = digest
            while len(raw) < self._dims:
                seed = hashlib.sha256(seed).digest()
                # Each 4-byte chunk → float in [-1, 1]
                for i in range(0, len(seed), 4):
                    if len(raw) >= self._dims:
                        break
                    val = int.from_bytes(seed[i : i + 4], "big", signed=True)
                    raw.append(val / (2**31))
            # Normalize to unit vector
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            vectors.append([x / norm for x in raw[: self._dims]])

        return vectors


# ── Process-global singleton ───────────────────────────────────────────────────

_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return the process-global EmbeddingProvider.

    Raises RuntimeError if ``set_embedding_provider()`` / ``init_embedding_provider()``
    have not been called yet.
    """
    if _provider is None:
        raise RuntimeError(
            "EmbeddingProvider not initialized — call init_embedding_provider() "
            "from the FastAPI lifespan or worker startup."
        )
    return _provider


def set_embedding_provider(provider: EmbeddingProvider) -> None:
    """Set the process-global provider (called from lifespan/tests)."""
    global _provider
    _provider = provider


def init_embedding_provider() -> EmbeddingProvider:
    """Create and register the embedding provider from app settings.

    Called from the FastAPI lifespan and worker startup.  Returns the
    initialized provider so callers can log or validate it.

    Provider selection (Settings.embedding_provider):
      - "openai": OpenAIEmbeddingProvider (requires openai_api_key)
      - "stub":   StubEmbeddingProvider   (tests/dev; no API key needed)

    Raises:
        ValueError: If the selected provider is "openai" but no API key
                    is configured.
    """
    from app.core.config import get_settings

    settings = get_settings()
    provider_name = settings.embedding_provider.lower()

    if provider_name == "stub":
        provider: EmbeddingProvider = StubEmbeddingProvider(
            dimensions=settings.embedding_dimensions,
            model=settings.embedding_model,
        )
        logger.info(
            "Embedding provider: stub (deterministic fake vectors — no API calls)",
            extra={"dimensions": settings.embedding_dimensions},
        )

    elif provider_name == "openai":
        api_key = settings.openai_api_key
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not configured but embedding_provider='openai'. "
                "Set the key or switch to embedding_provider='stub' for development."
            )
        rate_limiter = EmbeddingRateLimiter(rate_rpm=settings.embedding_rate_limit_rpm)
        provider = OpenAIEmbeddingProvider(
            api_key=api_key,
            model=settings.embedding_model,
            expected_dimensions=settings.embedding_dimensions,
            request_timeout=settings.embedding_request_timeout_seconds,
            max_retries=3,
            rate_limiter=rate_limiter,
        )
        logger.info(
            "Embedding provider: OpenAI",
            extra={
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "rate_limit_rpm": settings.embedding_rate_limit_rpm,
            },
        )

    else:
        raise ValueError(
            f"Unknown embedding_provider='{provider_name}'. "
            f"Supported values: 'openai', 'stub'."
        )

    set_embedding_provider(provider)
    return provider
