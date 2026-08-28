"""
Pydantic schemas for the /ask API (Phase 9).

POST /ask is the standalone orchestration endpoint (pre-conversation form):
it runs the full RAG pipeline and streams the answer as SSE — superseded by
the conversation endpoint in Phase 11, retained for testing and the
evaluation harness (roadmap Phase 9 §APIs).

The scope request shape is shared with POST /search (same VersionScope
resolution semantics — current-document/selected/knowledge-base map onto
the same three kinds).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.schemas.search import SearchScopeRequest


# ── Request ───────────────────────────────────────────────────────────────────

class AskHistoryMessage(BaseModel):
    """One prior conversation turn (bounded — the server keeps the last few)."""

    role: Literal["user", "assistant"]
    content: Annotated[
        str,
        Field(min_length=1, max_length=4000, description="The turn's text."),
    ]


class AskRequest(BaseModel):
    """POST /ask request body."""

    question: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="The user's question (the ORIGINAL words — the only "
                        "text ever answered or persisted; any retrieval rewrite "
                        "is an internal artifact).",
            examples=["What is the approval process?"],
        ),
    ]
    scope: SearchScopeRequest | None = Field(
        default=None,
        description=(
            "Optional document-scope narrowing (advisory hints in the "
            "question never EXPAND it — the authoritative scope is this "
            "selection intersected with the caller's permissions).  "
            "Omit for the entire knowledge base."
        ),
    )
    history: list[AskHistoryMessage] | None = Field(
        default=None,
        description=(
            "Recent conversation turns (max 6) enabling follow-up question "
            "rewriting.  No persistence happens in Phase 9 — the caller "
            "supplies the context."
        ),
    )
    top_k: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=20,
            description=(
                "Maximum sources considered for context (defaults to the "
                "server setting — the documented 5–8 band)."
            ),
        ),
    ] = None


# ── Response (SSE payloads) ───────────────────────────────────────────────────

class AskSourceItem(BaseModel):
    """One source in the ``sources`` SSE event."""

    index: int = Field(description="1-based SOURCE label the answer cites as [N].")
    chunk_id: str = Field(description="UUID of the source chunk (backend-resolved).")
    document_id: str = Field(description="UUID of the source document.")
    document_version_id: str = Field(description="UUID of the searched version.")
    document_name: str = Field(description="Human-readable document name.")
    page_number: int = Field(description="1-indexed page the chunk starts on.")
    section_title: str | None = Field(
        default=None, description="Section heading (null for unstructured documents)."
    )
    relevance: float = Field(ge=0.0, le=1.0, description="Presentation score [0, 1].")
    snippet: str = Field(description="First 500 characters of the source content.")


class AskDonePayload(BaseModel):
    """The terminal ``done`` SSE event payload."""

    groundedness: Literal["grounded", "partial", "ungrounded"] = Field(
        description=(
            "'grounded' when an evidence-constrained answer was generated; "
            "'ungrounded' is the explicit insufficient-evidence outcome — "
            "a SUCCESS state, not an error (Backend §48)."
        )
    )
    message: str | None = Field(
        default=None,
        description="User-facing note (set on the ungrounded path).",
    )
    model: str | None = Field(default=None, description="The generating model.")
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    intent: str = Field(default="QUESTION", description="Analyzer intent class.")
    topic: str | None = Field(default=None, description="Analyzer topic label (analytics).")
    used_rewrite: bool = Field(
        default=False,
        description="True when a drift-guarded standalone rewrite drove retrieval.",
    )
    used_reranker: bool = Field(default=False)
    latency_ms: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-stage latency_ms (analyzer/rewrite/retrieval/context/generation).",
    )
