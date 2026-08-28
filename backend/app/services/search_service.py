"""
Search service — thin orchestrator over the retrieval subsystem (Phase 8).

Backend §39: ``SearchService`` reuses ``rag/retriever.py`` and
``rag/hybrid_search.py`` (which in turn drives ``rag/reranker.py``) directly —
it deliberately STOPS before Context Assembly / LLM Generation (§33/§34).
Search's product purpose is to let users inspect ranked evidence directly,
not receive a synthesized answer.  Search and Chat share ~80% of the
pipeline and diverge only at the final stage — this service is the seam
where that divergence begins.

Responsibilities (kept deliberately thin):
  - Validate/normalize the mode toggle + filters coming from the API layer.
  - Delegate to HybridRetriever (Phase 7's SemanticRetriever remains
    available unchanged for its existing callers).
  - Surface degraded-mode signals (reranker fallback) to the API layer.

See:
  Backend-Architecture-Documentation.md §39 (Search Architecture)
  roadmap Phase 8 step 8
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.search import SearchFilters
from app.domain.versioning import VersionScope
from app.models.user import User
from app.rag.hybrid_search import HybridRetriever, HybridSearchOutcome, SearchMode

logger = logging.getLogger(__name__)

VALID_MODES: tuple[SearchMode, ...] = ("hybrid", "semantic", "keyword")


class SearchService:
    """Thin orchestrator — search stops before context assembly/LLM."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def search(
        self,
        *,
        query: str,
        user: User,
        scope: VersionScope | None = None,
        filters: SearchFilters | None = None,
        mode: str | None = None,
        top_k: int | None = None,
    ) -> HybridSearchOutcome:
        """Run a search in the requested mode.

        An unknown/blank mode falls back to the configured default rather
        than erroring — the API layer's schema validation is the first
        gate; this normalization is defense-in-depth for internal callers.
        """
        settings_mode: SearchMode = get_settings().search_mode_default
        effective_mode: SearchMode
        if mode and mode.lower() in VALID_MODES:
            effective_mode = mode.lower()  # type: ignore[assignment]
        else:
            if mode:
                logger.warning(
                    "SearchService: unknown mode=%r — falling back to default=%r",
                    mode, settings_mode,
                )
            effective_mode = settings_mode

        retriever = HybridRetriever(self._db)
        outcome = await retriever.search(
            query,
            user,
            scope=scope,
            filters=filters,
            mode=effective_mode,
            top_k=top_k,
        )

        if not outcome.used_reranker and outcome.mode in ("hybrid", "semantic"):
            # Degraded (fallback ordering) — visible in logs, surfaced to
            # the API layer via ``used_reranker=False`` + ``reranker_note``.
            logger.warning(
                "SearchService: reranker unavailable/failed (mode=%s, note=%s) — "
                "serving unreranked fused ordering",
                outcome.mode, outcome.reranker_note or "disabled-or-empty",
            )

        logger.info(
            "SearchService: query=%r org=%s mode=%s → %d results "
            "(vector=%d keyword=%d fused=%d reranked=%s dropped=%d)",
            query[:80], user.organization_id, outcome.mode, len(outcome.results),
            outcome.vector_branch_count, outcome.keyword_branch_count,
            outcome.fused_candidate_count, outcome.used_reranker,
            outcome.dropped_by_threshold,
        )
        return outcome
