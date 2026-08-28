"""
Reranker provider abstraction and concrete implementations (Phase 8).

Design rules (Backend §32; roadmap Phase 8 step 4) — mirrors embeddings.py:
  - Abstract interface: RerankerProvider.rerank(query, documents) -> scores
  - Concrete: CohereRerankerProvider (hosted cross-encoder API, V1 default)
  - Stub: StubRerankerProvider — deterministic lexical-overlap scoring for
    tests/dev; never calls an API.
  - "none": init_reranker_provider() returns None — reranking is skipped and
    the pipeline falls back to fused ordering (a configuration choice, not
    an error state).
  - Process-global singleton managed by get/set_reranker_provider().
  - All provider logic (retry, timeout, score normalization) lives here —
    nothing above the Infrastructure layer touches an SDK directly.
  - Provider scores are normalized to [0, 1] for consistent downstream
    thresholding regardless of the provider's native scale (Backend §32).

No I/O above this layer — callers depend on the abstract type only.

See:
  Backend-Architecture-Documentation.md §32 (Reranking)
  Backend-Architecture-Documentation.md §6/§34 (provider abstractions)
"""
from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from typing import Sequence

logger = logging.getLogger(__name__)


# ── Abstract base ─────────────────────────────────────────────────────────────

class RerankerProvider(ABC):
    """Abstract cross-encoder reranker provider.

    Implementers must be safe for concurrent async use.  The abstraction
    hides SDK imports, timeout/retry concerns, and score normalization from
    the rest of the application.
    """

    @abstractmethod
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """Score each document against the query.

        Args:
            query:     The search query text (raw text — cross-encoders
                       operate on raw text pairs, never embeddings).
            documents: Candidate chunk contents, in the caller's order.

        Returns:
            One relevance score per document, SAME ORDER as ``documents``,
            each normalized to [0.0, 1.0] (higher = more relevant).

        Raises:
            RerankerProviderError: For transient provider failures — the
                caller falls back to fused ordering (Backend §32).
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The canonical reranker model identifier (logged per search)."""


# ── Exceptions ────────────────────────────────────────────────────────────────

class RerankerProviderError(Exception):
    """Transient reranker failure — the pipeline falls back to RRF ordering
    (degraded, not failed — Backend §32/§51)."""

    def __init__(self, message: str, *, code: str = "RERANKER_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ── Score normalization helper ────────────────────────────────────────────────

def normalize_scores(raw_scores: Sequence[float]) -> list[float]:
    """Normalize provider scores to [0, 1].

    Handles the three shapes providers return:
      - already-[0,1] (Cohere returns 0..1) → clamped as-is
      - unbounded positive (e.g. logit-style) → squashed via a bounded
        min-max when the spread allows, else sigmoid
      - any-range signed → sigmoid squash keeps ordering and bounds

    Ordering is always preserved (monotonic transforms only) so the
    threshold policy and top-K truncation behave identically across
    providers.
    """
    if not raw_scores:
        return []
    bounded = all(0.0 <= s <= 1.0 for s in raw_scores)
    if bounded:
        return [max(0.0, min(1.0, float(s))) for s in raw_scores]
    return [1.0 / (1.0 + math.exp(-float(s))) for s in raw_scores]


# ── Cohere provider ───────────────────────────────────────────────────────────

class CohereRerankerProvider(RerankerProvider):
    """Cross-encoder reranking via the Cohere Rerank API.

    Handles:
    - Async client (cohere.AsyncClient)
    - Timeout (~5s — rerank latency is budgeted at 100–400 ms normally;
      the timeout absorbs provider load spikes, Backend §32)
    - 1 retry on transient failure, then RerankerProviderError → the
      caller's fallback path (Backend §51)

    The client is created lazily on first use so startup never blocks on
    the Cohere SDK (Backend §34 — provider clients initialised lazily).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-v3.5",
        request_timeout: float = 5.0,
        max_retries: int = 1,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._request_timeout = request_timeout
        self._max_retries = max_retries
        self._client = None  # lazy

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self):
        """Return the cached async client, creating it on first use."""
        if self._client is None:
            try:
                from cohere import AsyncClient  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RerankerProviderError(
                    "cohere package is not installed. Add 'cohere' to requirements.txt.",
                    code="RERANKER_PROVIDER_UNAVAILABLE",
                ) from exc
            self._client = AsyncClient(api_key=self._api_key, timeout=self._request_timeout)
        return self._client

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """Score documents against the query via Cohere Rerank."""
        if not documents:
            return []
        if not query or not query.strip():
            # A blank query has no signal — every document is equally
            # irrelevant; return neutral scores rather than calling the API.
            return [0.0 for _ in documents]

        client = self._get_client()

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await client.rerank(
                    model=self._model,
                    query=query,
                    documents=list(documents),
                    top_n=len(documents),
                    return_documents=False,
                )
                # Results are (index, relevance_score) pairs, possibly in
                # provider-ranked order — map back to the caller's order.
                scores = [0.0] * len(documents)
                for result in response.results:
                    scores[result.index] = float(result.relevance_score)
                return normalize_scores(scores)

            except Exception as exc:  # noqa: BLE001 — provider SDK raises broadly
                last_exc = exc
                exc_name = type(exc).__name__

                if "Unauthorized" in exc_name or "Forbidden" in exc_name or "PermissionDenied" in exc_name:
                    # Auth/permission failures are deterministic — retrying
                    # cannot help.  Degrade immediately (Backend §51: 4xx
                    # never retried).
                    logger.error("Reranker authentication failed: %s", exc)
                    raise RerankerProviderError(
                        f"Reranker authentication failed: {exc}",
                        code="RERANKER_AUTH_ERROR",
                    ) from exc

                logger.warning(
                    "Reranker call failed (%s, attempt %d/%d): %s",
                    exc_name, attempt, self._max_retries, exc,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(0.5 * attempt)

        raise RerankerProviderError(
            f"Reranker provider failed after {self._max_retries} attempt(s): {last_exc}",
        ) from last_exc


# ── Stub provider (tests / dev) ───────────────────────────────────────────────

class StubRerankerProvider(RerankerProvider):
    """Deterministic fake reranker — never calls any API.

    Scores are a pure lexical signal: the fraction of the query's
    alphanumeric terms that appear in the document text (case-insensitive).
    This approximates cross-encoder behaviour closely enough to exercise
    the threshold/fallback logic deterministically in tests:

      - documents sharing more query terms score higher
      - documents sharing none score 0.0 (below any sane threshold)
      - identical inputs always produce identical scores
    """

    def __init__(self, model: str = "stub-reranker") -> None:
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        import re

        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        scores: list[float] = []
        for doc in documents:
            if not terms:
                scores.append(0.0)
                continue
            doc_terms = set(re.findall(r"[a-z0-9]+", (doc or "").lower()))
            overlap = len(terms & doc_terms)
            scores.append(min(1.0, overlap / len(terms)))
        return scores


# ── Process-global singleton ──────────────────────────────────────────────────

_provider: RerankerProvider | None = None
_provider_initialized = False


def get_reranker_provider() -> RerankerProvider | None:
    """Return the process-global RerankerProvider, or None when reranking
    is disabled (``reranker_provider='none'``).

    Returns None (not an error) before initialization AND when disabled —
    callers treat None as "skip the rerank stage, use fused ordering".
    """
    return _provider


def set_reranker_provider(provider: RerankerProvider | None) -> None:
    """Set the process-global provider (called from lifespan/tests)."""
    global _provider, _provider_initialized
    _provider = provider
    _provider_initialized = True


def init_reranker_provider() -> RerankerProvider | None:
    """Create and register the reranker provider from app settings.

    Called from the FastAPI lifespan and worker startup.  Returns the
    initialized provider (or None when disabled) so callers can log it.

    Provider selection (Settings.reranker_provider):
      - "cohere": CohereRerankerProvider (requires cohere_api_key)
      - "stub":   StubRerankerProvider   (tests/dev; no API key needed)
      - "none":   reranking disabled — hybrid search runs unreranked

    Raises:
        ValueError: If the selected provider is "cohere" but no API key
                    is configured.
    """
    from app.core.config import get_settings

    settings = get_settings()
    provider_name = settings.reranker_provider.lower()

    if provider_name == "none":
        set_reranker_provider(None)
        logger.info("Reranker provider: none — hybrid search runs unreranked (RRF fallback ordering)")
        return None

    if provider_name == "stub":
        provider: RerankerProvider = StubRerankerProvider()
        logger.info("Reranker provider: stub (deterministic lexical-overlap scores — no API calls)")
        set_reranker_provider(provider)
        return provider

    if provider_name == "cohere":
        api_key = settings.cohere_api_key
        if not api_key:
            raise ValueError(
                "COHERE_API_KEY is not configured but reranker_provider='cohere'. "
                "Set the key or switch to reranker_provider='none' to disable reranking."
            )
        provider = CohereRerankerProvider(
            api_key=api_key,
            model=settings.reranker_model,
            request_timeout=settings.reranker_timeout_seconds,
            max_retries=settings.reranker_max_retries,
        )
        logger.info(
            "Reranker provider: Cohere",
            extra={
                "model": settings.reranker_model,
                "timeout_seconds": settings.reranker_timeout_seconds,
                "max_retries": settings.reranker_max_retries,
            },
        )
        set_reranker_provider(provider)
        return provider

    raise ValueError(
        f"Unknown reranker_provider='{provider_name}'. "
        f"Supported values: 'cohere', 'stub', 'none'."
    )
