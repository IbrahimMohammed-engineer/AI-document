"""
Semantic retriever — Phase 7 (Backend §22/§29; roadmap Phase 7 steps 7–9).

SemanticRetriever.search() is the first half of the RAG pipeline: given a
user's query and their permission context, it returns ranked, permission-
scoped, tenant-isolated chunk candidates.

The retriever enforces security rule #4 (retrieval-level isolation):
  1. Resolve the set of version IDs the user may access
     (AuthorizationService.resolve_allowed_documents).
  2. If the set is empty → return [] immediately (never attempt a wider search).
  3. Embed the query via EmbeddingProvider.
  4. ANN query with ALL permission predicates inside the SQL.

Phase 8 will extend this module with hybrid search (FTS branch + RRF fusion)
and reranking.  The interface is designed to accommodate those additions without
breaking Phase 7 callers.

See:
  Backend-Architecture-Documentation.md §22 (Embedding Pipeline)
  Backend-Architecture-Documentation.md §29 (Permission-Aware Retrieval)
  roadmap Phase 7 steps 7–11
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.versioning import VersionScope
from app.infrastructure.embeddings import EmbeddingProvider, get_embedding_provider
from app.models.user import User
from app.repositories.document_chunk_repository import (
    ChunkSearchResult,
    DocumentChunkRepository,
)
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)


# ── Public result type ────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A ranked chunk returned by the retriever.

    ``relevance`` is normalised to [0, 1].  Phase 7: cosine similarity.
    Phase 8: the normalized RERANKER score when reranking ran, the
    normalized fused (RRF) score on reranker fallback, the normalized
    keyword score in keyword mode, or cosine similarity in semantic mode
    (Backend §39 — user-facing relevance is never raw cosine in a mixed
    result set).
    """

    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str
    page_number: int
    section_title: str | None
    chunk_index: int
    snippet: str        # truncated content (first 500 chars)
    content: str        # full content (for context assembly in Phase 9)
    token_count: int
    relevance: float    # normalised [0, 1] — see docstring
    embedding_model: str | None
    metadata: dict
    # ── Phase 8 additions (optional — Phase 7 constructors unaffected) ────
    rerank_score: float | None = None   # cross-encoder score [0, 1] when reranked
    fused_score: float | None = None    # RRF fused score when hybrid ran
    keyword_score: float | None = None  # raw ts_rank in keyword mode


# ── Retriever ─────────────────────────────────────────────────────────────────

class SemanticRetriever:
    """Permission-aware semantic search using pgvector ANN (cosine similarity).

    Phase 7: vector-only search.
    Phase 8: hybrid (this class extended, not replaced).
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        """
        Args:
            db: Active async session (request-scoped or worker-scoped).
            embedding_provider: Override for tests; defaults to the process global.
        """
        self._db = db
        self._provider = embedding_provider or get_embedding_provider()
        self._settings = get_settings()

    async def search(
        self,
        query: str,
        user: User,
        *,
        scope: VersionScope | None = None,
        top_k: int | None = None,
    ) -> list[SearchResult]:
        """Run a permission-aware semantic search.

        Steps (Backend §29):
          1. resolve_allowed_documents → version_ids (empty → [])
          2. Embed the query
          3. ANN query with mandatory predicates
          4. Return ranked SearchResult list

        Args:
            query:  The user's search query (plain text, not yet embedded).
            user:   Authenticated user — org_id sourced from JWT (Backend §14).
            scope:  Optional search scope; None = all readable documents.
            top_k:  Max results; bounded by settings.search_top_k_max.

        Returns:
            List of SearchResult, highest relevance first.  May be empty.
        """
        if not query or not query.strip():
            logger.debug("SemanticRetriever.search: empty query — returning []")
            return []

        settings = self._settings
        effective_top_k = min(
            top_k if top_k is not None else settings.search_top_k_default,
            settings.search_top_k_max,
        )

        # ── Step 1: resolve permission scope ──────────────────────────────
        version_ids = await AuthorizationService.resolve_allowed_documents(
            user, self._db, scope=scope
        )
        if not version_ids:
            logger.info(
                "SemanticRetriever: empty scope for user=%s org=%s — short-circuiting",
                user.id, user.organization_id,
            )
            return []

        # ── Step 2: embed the query ────────────────────────────────────────
        try:
            query_vectors = await self._provider.embed([query.strip()])
        except Exception as exc:
            # Re-raise with a clearer message for the API layer
            logger.error("SemanticRetriever: query embedding failed: %s", exc)
            raise

        query_vector = query_vectors[0]

        # ── Step 3: ANN search with mandatory predicates ───────────────────
        chunk_repo = DocumentChunkRepository(self._db)
        raw_results: list[ChunkSearchResult] = await chunk_repo.semantic_search(
            organization_id=user.organization_id,
            version_ids=version_ids,
            query_vector=query_vector,
            top_k=effective_top_k,
            hnsw_ef_search=settings.hnsw_ef_search,
        )

        # ── Step 4: map to public result type ─────────────────────────────
        results = [
            SearchResult(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                document_version_id=r.document_version_id,
                document_name=r.document_name,
                page_id=r.page_id,
                page_number=r.page_number,
                section_title=r.section_title,
                chunk_index=r.chunk_index,
                snippet=r.content[:500],
                content=r.content,
                token_count=r.token_count,
                relevance=max(0.0, min(1.0, r.similarity)),  # clamp to [0,1]
                embedding_model=r.embedding_model,
                metadata=r.metadata,
            )
            for r in raw_results
        ]

        logger.info(
            "SemanticRetriever: query=%r org=%s scope=%s top_k=%d → %d results",
            query[:80], user.organization_id, scope.kind if scope else "all",
            effective_top_k, len(results),
        )
        return results
