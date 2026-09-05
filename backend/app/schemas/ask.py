"""
Pydantic schemas for the /ask API (Phases 9–10).

POST /ask is the standalone orchestration endpoint (pre-conversation form):
it runs the full RAG pipeline and streams the answer as SSE — superseded by
the conversation endpoint in Phase 11, retained for testing and the
evaluation harness (roadmap Phase 9 §APIs).

Phase 10: the terminal ``done`` payload now carries the resolved, validated
``citations[]`` (FE §12 Citation contract) plus the post-validation answer
text — citations are emitted only AFTER generation + validation complete,
never mid-stream (Backend §37).

The scope request shape is shared with POST /search (same VersionScope
resolution semantics — current-document/selected/knowledge-base map onto
the same three kinds).
"""
from __future__ import annotations

from datetime import date
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
    page_id: str = Field(description="UUID of the page the chunk starts on (citation anchor).")
    page_number: int = Field(description="1-indexed page the chunk starts on.")
    section_title: str | None = Field(
        default=None, description="Section heading (null for unstructured documents)."
    )
    relevance: float = Field(ge=0.0, le=1.0, description="Presentation score [0, 1].")
    snippet: str = Field(description="First 500 characters of the source content.")


class AskCitationItem(BaseModel):
    """One resolved, validated citation (the ``done`` payload's ``citations[]``).

    The FE §12 Citation contract: everything the badge/popover/navigation
    needs — the numbered index matching the inline [N] marker, the exact
    quoted span with char offsets (real source text, never model output —
    Backend §35), its surrounding context for the preview popover, and the
    document/version/page navigation target.
    """

    index: int = Field(description="The [N] ordinal as it appears inline in the answer.")
    chunk_id: str = Field(description="UUID of the exact chunk grounding this citation.")
    document_id: str = Field(description="UUID of the source document.")
    document_version_id: str = Field(description="UUID of the cited version.")
    document_name: str = Field(description="Human-readable document name.")
    version_number: int | None = Field(
        default=None, description="Version number of the cited version (badge display)."
    )
    effective_date: date | None = Field(
        default=None, description="The cited version's effective date, when set."
    )
    page_id: str = Field(description="UUID of the cited page (navigation anchor).")
    page: int = Field(description="1-indexed page number for direct viewer navigation.")
    section: str | None = Field(
        default=None, description="Denormalized section title/path (null if unstructured)."
    )
    text: str = Field(
        description=(
            "quoted_text — the exact source span from the ACTUAL chunk content "
            "(never model-paraphrased text mislabeled as a quote)."
        )
    )
    char_start: int | None = Field(
        default=None,
        description="Offset of the quoted span within the chunk content (highlight overlay).",
    )
    char_end: int | None = Field(default=None, description="End offset (exclusive).")
    context_before: str = Field(
        default="", description="Chunk text preceding the quoted span (preview popover)."
    )
    context_after: str = Field(
        default="", description="Chunk text following the quoted span (preview popover)."
    )
    relevance: float = Field(
        ge=0.0, le=1.0, description="Retrieval relevance of the cited chunk [0, 1]."
    )


class AskDonePayload(BaseModel):
    """The terminal ``done`` SSE event payload."""

    groundedness: Literal["grounded", "partial", "ungrounded"] = Field(
        description=(
            "'grounded' when an evidence-constrained, validated answer was "
            "generated; 'partial' when some claims were stripped or could not "
            "be verified; 'ungrounded' is the explicit insufficient-evidence "
            "outcome — a SUCCESS state, not an error (Backend §48)."
        )
    )
    message: str | None = Field(
        default=None,
        description="User-facing note (set on the ungrounded path).",
    )
    message_id: str | None = Field(
        default=None,
        description=(
            "UUID of the persisted assistant message (written atomically with "
            "its citations — Backend §50).  Null only if persistence failed."
        ),
    )
    answer: str = Field(
        default="",
        description=(
            "The FINAL post-validation answer text.  May differ from the "
            "accumulated token stream: uncited/unsupported claims were "
            "stripped and invalid reference markers removed — render THIS."
        ),
    )
    citations: list[AskCitationItem] = Field(
        default_factory=list,
        description="Resolved, validated citations (never streamed mid-generation).",
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
    regenerated: bool = Field(
        default=False,
        description=(
            "True when the shipped answer is a citation-emphasis regeneration "
            "(one bounded retry — Backend §36)."
        ),
    )
    stripped_claims: int = Field(
        default=0, description="Claims removed by validation (uncited/unsupported)."
    )
    entailment_checks: int = Field(
        default=0, description="Entailment verification calls made for this answer."
    )
    injection_attempt: bool = Field(
        default=False,
        description=(
            "Phase 16: True when the canary sentinel fired — a document "
            "attempted instruction hijack; contaminated sentence(s) were "
            "stripped. FE shows a subtle security notice."
        ),
    )
    latency_ms: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-stage latency_ms (analyzer/rewrite/retrieval/context/"
            "generation/citations)."
        ),
    )
