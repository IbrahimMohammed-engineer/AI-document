"""
Reranking orchestration — threshold policy + graceful fallback (Phase 8).

Sits between hybrid search and the raw provider (Backend §32):
    rag/reranker.py (this file)  →  infrastructure/reranker.py (RerankerProvider)

Responsibilities:
  - Call the provider over the fused candidates' CONTENT (raw text —
    cross-encoders operate on text pairs, never embeddings).
  - Normalize provider scores to [0, 1] and attach them to candidates.
  - THRESHOLD POLICY: candidates below ``rerank_score_threshold`` are
    DROPPED even if within the top-K by rank — this is what makes "zero
    usable chunks" a reachable, honest outcome for the insufficient-
    evidence path (Backend §32; Phase 9/10 depend on it).
  - FALLBACK: provider unavailable or failing → return the candidates
    unreranked in fused order with ``used_reranker=False`` (degraded,
    never failed — Backend §32/§51).  The request proceeds with the
    lower-confidence fused signal.

See:
  Backend-Architecture-Documentation.md §32 (Reranking)
  Backend-Architecture-Documentation.md §51 (external service failures)
  roadmap Phase 8 steps 3–6
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.infrastructure.reranker import (
    RerankerProvider,
    RerankerProviderError,
)
from app.rag.retriever import SearchResult

logger = logging.getLogger(__name__)


# ── Outcome type ──────────────────────────────────────────────────────────────

@dataclass
class RerankOutcome:
    """Result of the rerank stage.

    ``results`` is re-sorted (score descending) and threshold-filtered
    when the reranker ran; otherwise it is the input, unchanged, in fused
    order (fallback).
    """

    results: list[SearchResult]
    used_reranker: bool
    dropped_count: int = 0      # candidates removed by the score threshold
    reranker_error: str | None = field(default=None)  # set on fallback-by-error


# ── Stage ─────────────────────────────────────────────────────────────────────

async def rerank_candidates(
    query: str,
    candidates: list[SearchResult],
    *,
    provider: RerankerProvider | None,
    threshold: float,
) -> RerankOutcome:
    """Rerank fused candidates; fall back to fused order on any failure.

    Args:
        query:      The search query text (the retrieval query — in Phase 9
                    this is the rewritten standalone query; for search it is
                    the raw user query).
        candidates: Fused candidate list (fused score already stored on
                    each result's ``fused_score``).
        provider:   The process-global RerankerProvider — ``None`` means
                    reranking is disabled (config) → fallback ordering.
        threshold:  Minimum normalized score [0,1] to survive.  Candidates
                    below it are dropped EVEN IF within top-K by rank
                    (Backend §32 — the honest-empty-samples guarantee).

    Returns:
        RerankOutcome — never raises for provider failures (only for
        programmer errors): a reranker outage degrades ordering, it never
        fails the request (Backend §32 fallback rule).
    """
    if not candidates:
        return RerankOutcome(results=[], used_reranker=False)

    # ── Disabled (config) or unreachable — fused-order fallback ───────────
    if provider is None:
        return RerankOutcome(results=candidates, used_reranker=False)

    # ── Call the provider over raw candidate content ───────────────────────
    try:
        scores = await provider.rerank(query, [c.content for c in candidates])
    except RerankerProviderError as exc:
        logger.warning(
            "Rerank stage: provider failed (%s) — falling back to unreranked "
            "fused ordering (degraded, not failed)",
            exc.code,
        )
        return RerankOutcome(
            results=candidates,
            used_reranker=False,
            reranker_error=exc.code,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the request here
        logger.warning(
            "Rerank stage: unexpected provider error (%s) — falling back to "
            "unreranked fused ordering (degraded, not failed)",
            exc,
        )
        return RerankOutcome(
            results=candidates,
            used_reranker=False,
            reranker_error="RERANKER_UNEXPECTED_ERROR",
        )

    if len(scores) != len(candidates):
        # Misbehaving provider — treat as unavailability (fail-safe path)
        logger.warning(
            "Rerank stage: provider returned %d scores for %d candidates — "
            "falling back to fused ordering",
            len(scores), len(candidates),
        )
        return RerankOutcome(
            results=candidates,
            used_reranker=False,
            reranker_error="RERANKER_SCORE_COUNT_MISMATCH",
        )

    # ── Attach scores, apply threshold, re-sort ────────────────────────────
    scored: list[tuple[SearchResult, float]] = list(
        zip(candidates, scores, strict=True)
    )
    surviving: list[SearchResult] = []
    dropped = 0
    for result, score in scored:
        if score < threshold:
            dropped += 1
            continue
        result.rerank_score = score
        # Presentation rule (Backend §39): user-facing relevance is the
        # normalized reranker score — never raw cosine.
        result.relevance = score
        surviving.append(result)

    surviving.sort(key=lambda r: r.rerank_score or 0.0, reverse=True)

    if dropped:
        logger.debug(
            "Rerank stage: %d/%d candidates below threshold %.2f — dropped",
            dropped, len(candidates), threshold,
        )

    return RerankOutcome(
        results=surviving,
        used_reranker=True,
        dropped_count=dropped,
    )
