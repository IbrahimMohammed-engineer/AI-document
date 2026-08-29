"""
Hybrid search — dual-branch retrieval, RRF fusion, mode toggle (Phase 8).

Implements exactly the orchestration documented in Backend §31 and the
query pattern in DB §18:

    Question (query text + its embedding)
            │
      ┌─────┴──────┐
      ▼            ▼
  Vector Search  Full-Text Search      ← BOTH within the mandatory
  (pgvector,     (tsvector,             permission/metadata scope —
   cosine)        websearch_to_tsquery) the SAME scope resolution feeds
      │            │                    both branches (Backend §29/§30)
      └─────┬──────┘
            ▼
    RRF Fusion (k=60, computed HERE in Python — easily testable/tunable,
            not entangled in SQL; Backend §31)
            ↓ top 20–30 candidates
    Reranker (rag/reranker.py → infrastructure/reranker.py)
            ↓ score < threshold → dropped
    final top 5–8 → search response (Phase 9: context assembly)

MODE TOGGLE (Backend §39): one parameter that skips fusion branches —
  - "hybrid":   vector + keyword → RRF → rerank → threshold
  - "semantic": vector only → rerank → threshold (FTS branch skipped)
  - "keyword":  keyword only, NO reranking (reranking's value is
                specifically in refining semantically-retrieved candidates)
NOT three separately implemented search paths.

All candidate counts are config values (Backend §31) — tuned by Phase 18's
evaluation evidence, never hardcoded magic numbers.

See:
  Backend-Architecture-Documentation.md §29 (Permission-Aware Retrieval)
  Backend-Architecture-Documentation.md §30 (Metadata Filtering)
  Backend-Architecture-Documentation.md §31 (Hybrid Search)
  Backend-Architecture-Documentation.md §32 (Reranking)
  roadmap Phase 8 steps 1–9
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.search import SearchFilters
from app.domain.versioning import VersionScope
from app.infrastructure.embeddings import EmbeddingProvider, get_embedding_provider
from app.infrastructure.reranker import get_reranker_provider
from app.models.user import User
from app.rag.retriever import SearchResult
from app.rag.reranker import rerank_candidates
from app.repositories.document_chunk_repository import (
    ChunkSearchResult,
    DocumentChunkRepository,
)
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)

SearchMode = Literal["hybrid", "semantic", "keyword"]


# ── RRF fusion (pure — unit-testable in isolation, Backend §31) ───────────────

@dataclass
class FusedCandidate:
    """One chunk after Reciprocal Rank Fusion."""

    chunk: ChunkSearchResult
    fused_score: float
    vector_rank: int | None   # 1-based rank in the vector branch (None = absent)
    keyword_rank: int | None  # 1-based rank in the keyword branch (None = absent)


def rrf_fuse(
    vector_results: Sequence[ChunkSearchResult],
    keyword_results: Sequence[ChunkSearchResult],
    *,
    k: int = 60,
) -> list[FusedCandidate]:
    """Reciprocal Rank Fusion: ``score = Σ 1 / (k + rank)`` per branch.

    Both input lists MUST already be ranked best-first (each branch's SQL
    guarantees this).  Ranks are 1-based; a chunk absent from a branch
    contributes nothing from that branch.  ``k`` dampens the influence of
    top ranks so a #1 hit on one branch cannot dominate a chunk both
    branches agree on (DB §18; k=60 is the standard constant).

    Pure function — no I/O, deterministic, trivially unit-testable
    (Backend §58: fusion math is tested independently of SQL).
    """
    scores: dict[str, FusedCandidate] = {}

    for rank, chunk in enumerate(vector_results, start=1):
        scores[chunk.chunk_id] = FusedCandidate(
            chunk=chunk,
            fused_score=1.0 / (k + rank),
            vector_rank=rank,
            keyword_rank=None,
        )

    for rank, chunk in enumerate(keyword_results, start=1):
        existing = scores.get(chunk.chunk_id)
        contribution = 1.0 / (k + rank)
        if existing is None:
            scores[chunk.chunk_id] = FusedCandidate(
                chunk=chunk,
                fused_score=contribution,
                vector_rank=None,
                keyword_rank=rank,
            )
        else:
            existing.fused_score += contribution
            existing.keyword_rank = rank

    fused = sorted(scores.values(), key=lambda c: c.fused_score, reverse=True)
    return fused


def normalize_minmax(values: Sequence[float]) -> list[float]:
    """Min-max normalize scores to [0, 1] for presentation (keyword branch).

    ``ts_rank`` is unbounded and corpus-dependent; only relative magnitude
    is meaningful.  Constant sets (all-equal scores) map to 1.0 — a set of
    equally-matching keyword hits is genuinely a full-strength match set.
    Empty input → empty list.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


# ── Outcome type ──────────────────────────────────────────────────────────────

@dataclass
class HybridSearchOutcome:
    """Everything the search API (and Phase 9's RAG pipeline) needs."""

    results: list[SearchResult]
    mode: SearchMode
    used_reranker: bool
    dropped_by_threshold: int = 0
    vector_branch_count: int = 0
    keyword_branch_count: int = 0
    fused_candidate_count: int = 0
    reranker_note: str | None = field(default=None)  # fallback reason, if any


# ── Retriever ─────────────────────────────────────────────────────────────────

class HybridRetriever:
    """Permission-aware hybrid retrieval: vector + keyword + RRF + rerank.

    Security contract (identical to Phase 7's SemanticRetriever):
      1. resolve_allowed_documents → version_ids (empty → short-circuit,
         NEVER a broader fallback).
      2. Both branch queries embed the full mandatory predicate set
         (org + allowed versions + not-deleted) INSIDE the SQL.
      3. One scope resolution feeds BOTH branches — identical predicates
         (Backend §29/§30 business rule).
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        reranker_provider: object | None = None,  # RerankerProvider | None; None = process global
    ) -> None:
        """
        Args:
            db: Active async session.
            embedding_provider: Override for tests; defaults to the process global.
            reranker_provider:  Override for tests.  NOTE the sentinel
                distinction: leaving the default resolves the process
                global (which itself may be None = reranking disabled);
                passing an explicit None also means "no reranker".  Tests
                that want the disabled behaviour pass ``reranker_provider=None``
                after :func:`set_reranker_provider` — or simply rely on the
                global being None.
        """
        self._db = db
        self._embedding = embedding_provider or get_embedding_provider()
        self._explicit_reranker = reranker_provider
        self._settings = get_settings()

    def _reranker(self):
        """Resolve the reranker provider (explicit override wins)."""
        if self._explicit_reranker is not None:
            return self._explicit_reranker
        return get_reranker_provider()

    async def search(
        self,
        query: str,
        user: User,
        *,
        scope: VersionScope | None = None,
        filters: SearchFilters | None = None,
        mode: SearchMode = "hybrid",
        top_k: int | None = None,
    ) -> HybridSearchOutcome:
        """Run permission-aware retrieval in the requested mode.

        Args:
            query:   The user's search query (plain text).
            user:    Authenticated user — org from JWT (Backend §14).
            scope:   Optional scope; None = all readable documents.
            filters: Optional metadata filters (Backend §30) — applied as
                     WHERE clauses INSIDE both branch queries.
            mode:    "hybrid" | "semantic" | "keyword" (Backend §39).
            top_k:   Max FINAL results; bounded by settings.search_top_k_max.

        Returns:
            HybridSearchOutcome — results ranked best-first; ``relevance``
            is the presentation score per Backend §39 (never raw cosine).
        """
        if not query or not query.strip():
            logger.debug("HybridRetriever.search: empty query — returning []")
            return HybridSearchOutcome(
                results=[], mode=mode, used_reranker=False
            )

        settings = self._settings
        effective_top_k = min(
            top_k if top_k is not None else settings.search_top_k_default,
            settings.search_top_k_max,
        )

        # ── Step 1: resolve permission scope (ONE resolution feeds both) ──
        version_ids = await AuthorizationService.resolve_allowed_documents(
            user, self._db, scope=scope
        )
        if not version_ids:
            logger.info(
                "HybridRetriever: empty scope for user=%s org=%s — short-circuiting",
                user.id, user.organization_id,
            )
            return HybridSearchOutcome(
                results=[], mode=mode, used_reranker=False
            )

        chunk_repo = DocumentChunkRepository(self._db)
        query_text = query.strip()

        # ── Step 2: run the branch/mode pipeline ───────────────────────────
        if mode == "keyword":
            return await self._keyword_only(
                chunk_repo, query_text, user, version_ids, filters,
                effective_top_k,
            )

        if mode == "semantic":
            return await self._semantic_only(
                chunk_repo, query_text, user, version_ids, filters,
                effective_top_k,
            )

        return await self._hybrid(
            chunk_repo, query_text, user, version_ids, filters,
            effective_top_k,
        )

    # ── Mode implementations ───────────────────────────────────────────────

    async def _embed_query(self, query_text: str) -> list[float]:
        vectors = await self._embedding.embed([query_text])
        return vectors[0]

    async def _semantic_only(
        self,
        chunk_repo: DocumentChunkRepository,
        query_text: str,
        user: User,
        version_ids: list[str],
        filters: SearchFilters | None,
        top_k: int,
    ) -> HybridSearchOutcome:
        """Semantic mode: vector branch only → rerank → threshold."""
        query_vector = await self._embed_query(query_text)
        raw = await chunk_repo.semantic_search(
            organization_id=user.organization_id,
            version_ids=version_ids,
            query_vector=query_vector,
            top_k=self._settings.hybrid_vector_top_k,
            hnsw_ef_search=self._settings.hnsw_ef_search,
            filters=filters,
        )

        candidates = [self._to_search_result(r, relevance=_clamp01(r.similarity)) for r in raw]
        outcome = await rerank_candidates(
            query_text, candidates,
            provider=self._reranker(),  # type: ignore[arg-type]
            threshold=self._settings.rerank_score_threshold,
        )
        return HybridSearchOutcome(
            results=outcome.results[:top_k],
            mode="semantic",
            used_reranker=outcome.used_reranker,
            dropped_by_threshold=outcome.dropped_count,
            vector_branch_count=len(raw),
            reranker_note=outcome.reranker_error,
        )

    async def _keyword_only(
        self,
        chunk_repo: DocumentChunkRepository,
        query_text: str,
        user: User,
        version_ids: list[str],
        filters: SearchFilters | None,
        top_k: int,
    ) -> HybridSearchOutcome:
        """Keyword mode: FTS branch only, NO reranking (Backend §39)."""
        raw = await chunk_repo.keyword_search(
            organization_id=user.organization_id,
            version_ids=version_ids,
            query_text=query_text,
            top_k=self._settings.hybrid_keyword_top_k,
            filters=filters,
        )

        # Presentation: min-max normalized ts_rank (raw rank is unbounded).
        normalized = normalize_minmax([r.similarity for r in raw])
        results = [
            self._to_search_result(
                chunk,
                relevance=score,
                keyword_score=chunk.similarity,
            )
            for chunk, score in zip(raw, normalized, strict=True)
        ]
        return HybridSearchOutcome(
            results=results[:top_k],
            mode="keyword",
            used_reranker=False,  # by design, not a degradation
            keyword_branch_count=len(raw),
        )

    async def _hybrid(
        self,
        chunk_repo: DocumentChunkRepository,
        query_text: str,
        user: User,
        version_ids: list[str],
        filters: SearchFilters | None,
        top_k: int,
    ) -> HybridSearchOutcome:
        """Hybrid mode: both branches → RRF → candidate selection → rerank."""
        settings = self._settings

        # Embed once; both branches consume the same query.
        query_vector = await self._embed_query(query_text)

        vector_raw = await chunk_repo.semantic_search(
            organization_id=user.organization_id,
            version_ids=version_ids,
            query_vector=query_vector,
            top_k=settings.hybrid_vector_top_k,
            hnsw_ef_search=settings.hnsw_ef_search,
            filters=filters,
        )
        keyword_raw = await chunk_repo.keyword_search(
            organization_id=user.organization_id,
            version_ids=version_ids,
            query_text=query_text,
            top_k=settings.hybrid_keyword_top_k,
            filters=filters,
        )

        # ── RRF fusion (in Python — Backend §31) ───────────────────────────
        fused = rrf_fuse(vector_raw, keyword_raw, k=settings.rrf_k)

        # ── Candidate selection: top 20–30 → reranker (Backend §31) ───────
        candidates = fused[: settings.rerank_candidate_count]

        # Normalized fused score doubles as the fallback presentation score
        # (Backend §39: fused score, never raw cosine, on fallback).
        fused_normalized = normalize_minmax([c.fused_score for c in candidates])
        candidates_search: list[SearchResult] = [
            self._to_search_result(
                c.chunk,
                relevance=score,
                fused_score=c.fused_score,
            )
            for c, score in zip(candidates, fused_normalized, strict=True)
        ]

        if not candidates_search:
            return HybridSearchOutcome(
                results=[],
                mode="hybrid",
                used_reranker=False,
                vector_branch_count=len(vector_raw),
                keyword_branch_count=len(keyword_raw),
                fused_candidate_count=0,
            )

        # ── Rerank stage (threshold + fallback inside) ─────────────────────
        rerank_outcome = await rerank_candidates(
            query_text, candidates_search,
            provider=self._reranker(),  # type: ignore[arg-type]
            threshold=settings.rerank_score_threshold,
        )

        return HybridSearchOutcome(
            results=rerank_outcome.results[:top_k],
            mode="hybrid",
            used_reranker=rerank_outcome.used_reranker,
            dropped_by_threshold=rerank_outcome.dropped_count,
            vector_branch_count=len(vector_raw),
            keyword_branch_count=len(keyword_raw),
            fused_candidate_count=len(candidates_search),
            reranker_note=rerank_outcome.reranker_error,
        )

    # ── Mapping helper ─────────────────────────────────────────────────────

    @staticmethod
    def _to_search_result(
        chunk: ChunkSearchResult,
        *,
        relevance: float,
        fused_score: float | None = None,
        keyword_score: float | None = None,
    ) -> SearchResult:
        """Map a repository row to the public result type."""
        return SearchResult(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version_id=chunk.document_version_id,
            document_name=chunk.document_name,
            page_id=chunk.page_id,
            page_number=chunk.page_number,
            section_title=chunk.section_title,
            chunk_index=chunk.chunk_index,
            snippet=chunk.content[:500],
            content=chunk.content,
            token_count=chunk.token_count,
            relevance=_clamp01(relevance),
            embedding_model=chunk.embedding_model,
            metadata=chunk.metadata,
            fused_score=fused_score,
            keyword_score=keyword_score,
        )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
