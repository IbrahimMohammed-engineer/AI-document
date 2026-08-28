"""
Pydantic schemas for the search API.

Phase 7: POST /search (semantic only), GET /documents/{id}/chunks.
Phase 8: POST /search extended with the mode toggle
  (hybrid — default | semantic | keyword) and the metadata filter set
  (document_types / collection_ids / department / owner_id) plus the
  temporal scope (scope.as_of → effective-version resolution, Backend §30).
  The endpoint contract remains backward-compatible: every Phase 8 field
  is optional and defaults to the Phase 7 behaviour shape.

See:
  Backend-Architecture-Documentation.md §30 (Metadata Filtering)
  Backend-Architecture-Documentation.md §39 (Search Architecture)
  roadmap Phase 8 steps 7–9
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.search import ALLOWED_DOCUMENT_TYPES

SearchMode = Literal["hybrid", "semantic", "keyword"]


# ── Request ───────────────────────────────────────────────────────────────────

class SearchScopeRequest(BaseModel):
    """Optional scope narrowing for a search request.

    Exactly one of ``document_ids`` or ``collection_ids`` should be set;
    if both are empty/absent the scope defaults to all readable documents.

    ``as_of`` (Phase 8, Backend §30): an ISO-8601 point-in-time — when set,
    the effective version AT THAT MOMENT is searched per document (temporal
    questions resolve through the same domain function as "current"
    resolution).  Documents with no version effective then are excluded.
    """

    document_ids: list[str] | None = Field(
        default=None,
        description="Restrict search to these document IDs (must belong to the caller's org).",
        examples=[["doc-uuid-1", "doc-uuid-2"]],
    )
    collection_ids: list[str] | None = Field(
        default=None,
        description="Restrict search to documents in these collection IDs.",
        examples=[["coll-uuid-1"]],
    )
    as_of: Annotated[
        str | None,
        Field(
            default=None,
            max_length=40,
            description=(
                "ISO-8601 point-in-time for temporal scope "
                "(e.g. '2025-12-31' or '2025-06-01T00:00:00Z'). "
                "Omit to search each document's current version."
            ),
        ),
    ] = None

    @field_validator("document_ids", "collection_ids", mode="before")
    @classmethod
    def _strip_empty(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) == 0:
            return None
        return v


class SearchFiltersRequest(BaseModel):
    """Metadata filters (Phase 8, Backend §30) — combined with AND.

    Applied inside the retrieval SQL on BOTH branches (never as a
    post-retrieval filter).  Unknown document_type values are rejected
    with 422 — they could otherwise only ever match nothing.
    """

    document_types: list[str] | None = Field(
        default=None,
        description="Restrict to documents of these types.",
        examples=[["policy", "contract"]],
    )
    collection_ids: list[str] | None = Field(
        default=None,
        description="Restrict to documents belonging to these collection IDs.",
    )
    department: Annotated[
        str | None,
        Field(default=None, max_length=200, description="Restrict to documents owned by this department."),
    ] = None
    owner_id: str | None = Field(
        default=None,
        description="Restrict to documents owned by this user ID.",
    )

    @field_validator("document_types", "collection_ids", mode="before")
    @classmethod
    def _strip_empty(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) == 0:
            return None
        return v

    @field_validator("document_types")
    @classmethod
    def _validate_document_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        unknown = [t for t in v if t not in ALLOWED_DOCUMENT_TYPES]
        if unknown:
            raise ValueError(
                f"Unknown document_type value(s): {unknown}. "
                f"Allowed: {sorted(ALLOWED_DOCUMENT_TYPES)}."
            )
        return v


class SearchRequest(BaseModel):
    """POST /search request body."""

    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="The search query (plain text).",
            examples=["What is the document retention policy?"],
        ),
    ]
    scope: SearchScopeRequest | None = Field(
        default=None,
        description=(
            "Optional scope filter. "
            "Omit to search all documents the user can access."
        ),
    )
    mode: SearchMode | None = Field(
        default=None,
        description=(
            "Search mode (Phase 8): 'hybrid' (default — vector + keyword "
            "fused via RRF, then reranked), 'semantic' (vector only, "
            "reranked), or 'keyword' (full-text only, never reranked)."
        ),
    )
    filters: SearchFiltersRequest | None = Field(
        default=None,
        description=(
            "Optional metadata filters (Phase 8): document types, "
            "collections, department, owner.  Combined with AND."
        ),
    )
    top_k: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=50,
            description=(
                "Maximum number of results to return (1–50). "
                "Defaults to the server setting (typically 10)."
            ),
        ),
    ] = None


# ── Response ──────────────────────────────────────────────────────────────────

class SearchResultItem(BaseModel):
    """A single ranked search result."""

    chunk_id: str = Field(description="UUID of the matching document chunk.")
    document_id: str = Field(description="UUID of the source document.")
    document_version_id: str = Field(description="UUID of the document version searched.")
    document_name: str = Field(description="Human-readable document name.")
    page_number: int = Field(description="1-indexed page the chunk starts on.")
    section_title: str | None = Field(
        default=None,
        description="Section heading of the chunk (null if the document has no structure).",
    )
    chunk_index: int = Field(description="0-based reading-order index within the version.")
    snippet: str = Field(description="First 500 characters of the chunk content.")
    relevance: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Presentation score [0, 1] — the normalized reranker score when "
            "reranking ran, the normalized fused score on reranker fallback, "
            "or the normalized single-branch score.  NEVER raw cosine "
            "(Backend §39)."
        ),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Structural extras: heading path, table/list flags, bounding boxes.",
    )


class SearchResponse(BaseModel):
    """POST /search response envelope."""

    results: list[SearchResultItem] = Field(
        description="Ranked search results, highest relevance first."
    )
    total: int = Field(description="Number of results returned.")
    query: str = Field(description="The original search query (echo for client convenience).")
    scope_kind: str = Field(
        description="Effective scope kind used: 'all', 'documents', or 'collections'."
    )
    mode: SearchMode = Field(
        default="hybrid",
        description="The effective search mode used for this response.",
    )
    used_reranker: bool = Field(
        default=False,
        description=(
            "True when cross-encoder reranking produced the final ordering. "
            "False in keyword mode (by design) or when the reranker was "
            "unavailable (degraded — fused ordering served instead)."
        ),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Human-readable message (e.g. 'No documents in scope', or a "
            "degraded-mode notice when the reranker fell back)."
        ),
    )

