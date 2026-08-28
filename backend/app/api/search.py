"""
Search API router — Phase 7/8.

Endpoints:
  POST /search        — Permission-aware search (hybrid/semantic/keyword).
  GET  /documents/{document_id}/chunks
                      — Debug/internal: list chunks for a document version.

Security: all endpoints require authentication.  The search endpoint passes
the user's organisation and resolved allowed-version set into the retrieval
layer — the scope predicate is NEVER broadened by client input (Backend
§14/§29).  Metadata filters ride INTO the retrieval SQL as additional
predicates — never as post-retrieval filtering (Backend §30).

Phase 8 extends POST /search with:
  - mode toggle (hybrid default / semantic / keyword) — one parameter that
    skips fusion branches, not three search implementations (Backend §39)
  - metadata filters (type, department, collection, owner) + temporal
    scope (as_of → effective-version resolution)
  - cross-encoder reranking with score threshold and graceful fallback
    (reranker outage degrades ordering; it never fails the request)
The endpoint contract is stable from Phase 7 onward; Phase 18's evaluation
harness uses this same endpoint for its runs.

See:
  Backend-Architecture-Documentation.md §29 (Permission-Aware Retrieval)
  Backend-Architecture-Documentation.md §30 (Metadata Filtering)
  Backend-Architecture-Documentation.md §31 (Hybrid Search)
  Backend-Architecture-Documentation.md §39 (Search Architecture)
  roadmap Phase 8 steps 7–10
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.core.exceptions import ExternalServiceError
from app.domain.search import SearchFilters
from app.domain.versioning import VersionScope
from app.infrastructure.embeddings import EmbeddingProviderError
from app.models.user import User
from app.schemas.search import (
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from app.services.search_service import SearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])
chunks_router = APIRouter(tags=["documents"])


# ── POST /search ──────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=SearchResponse,
    summary="Hybrid search over documents (vector + keyword + rerank)",
    description=(
        "Searches document chunks in the requested mode: hybrid (vector + "
        "full-text fused via Reciprocal Rank Fusion, then cross-encoder "
        "reranked — default), semantic (vector only), or keyword "
        "(full-text only).  Results are scoped to documents the "
        "authenticated user has permission to read; metadata filters are "
        "applied inside the retrieval query itself.  An empty result set "
        "means no relevant chunks were found — it is never a permission "
        "error."
    ),
)
async def search(
    request: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """POST /search — permission-aware hybrid search."""
    # ── Resolve the request scope ──────────────────────────────────────────
    scope: VersionScope
    if request.scope is None:
        scope = VersionScope.all_documents()
        scope_kind = "all"
    elif request.scope.document_ids:
        scope = VersionScope.for_documents(request.scope.document_ids)
        scope_kind = "documents"
    elif request.scope.collection_ids:
        scope = VersionScope.for_collections(request.scope.collection_ids)
        scope_kind = "collections"
    else:
        scope = VersionScope.all_documents()
        scope_kind = "all"

    # Temporal scope (Backend §30) — the effective version at the asked time
    if request.scope is not None and request.scope.as_of:
        scope = VersionScope(
            kind=scope.kind,
            document_ids=scope.document_ids,
            collection_ids=scope.collection_ids,
            as_of=request.scope.as_of,
        )

    # ── Metadata filters (Backend §30) ─────────────────────────────────────
    filters = SearchFilters.empty()
    if request.filters is not None:
        filters = SearchFilters(
            document_types=tuple(request.filters.document_types or ()),
            collection_ids=tuple(request.filters.collection_ids or ()),
            department=request.filters.department,
            owner_id=request.filters.owner_id,
        )

    # ── Run the search service ─────────────────────────────────────────────
    try:
        service = SearchService(db)
        outcome = await service.search(
            query=request.query,
            user=current_user,
            scope=scope,
            filters=filters,
            mode=request.mode,
            top_k=request.top_k,
        )
    except EmbeddingProviderError as exc:
        # Transient provider failure → 503 (retryable by the client)
        logger.error(
            "Embedding provider error during search: %s (user=%s)",
            exc, current_user.id,
        )
        raise ExternalServiceError(
            "The embedding service is temporarily unavailable. "
            "Please try again in a moment.",
        ) from exc

    # ── Build response ─────────────────────────────────────────────────────
    message: str | None = None
    if not outcome.results:
        # Distinguish "no readable documents" from "no matching chunks"
        message = (
            "No documents in scope."
            if scope_kind != "all"
            else "No relevant results found."
        )
    elif not outcome.used_reranker and outcome.mode in ("hybrid", "semantic"):
        # Degraded, not failed (Backend §32 fallback rule) — tell the client
        message = (
            "Reranking is temporarily unavailable — results are ordered by "
            "fused relevance."
        )

    return SearchResponse(
        results=[
            SearchResultItem(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                document_version_id=r.document_version_id,
                document_name=r.document_name,
                page_number=r.page_number,
                section_title=r.section_title,
                chunk_index=r.chunk_index,
                snippet=r.snippet,
                relevance=r.relevance,
                metadata=r.metadata,
            )
            for r in outcome.results
        ],
        total=len(outcome.results),
        query=request.query,
        scope_kind=scope_kind,
        mode=outcome.mode,
        used_reranker=outcome.used_reranker,
        message=message,
    )
