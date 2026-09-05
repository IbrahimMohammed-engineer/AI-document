"""
Pydantic schemas for the chat API (Phase 11).

Endpoints covered:
  - POST /chat/conversations             (lazy creation + first message → SSE)
  - GET  /chat/conversations             (paginated, recency-ordered)
  - GET  /chat/conversations/{id}        (conversation + messages + citations)
  - POST /chat/conversations/{id}/messages (SSE stream)
  - DELETE /chat/conversations/{id}      (archive — soft delete)
  - POST /chat/messages/{id}/stop        (explicit cancellation)
  - POST /chat/messages/{id}/feedback    (±1 upsert)

The SSE event payloads mirror the /ask shapes (AskSourceItem/AskCitationItem
are reused) plus the chat-specific ``start`` event and the terminal
``messageId``/``groundedness``/``stopped`` fields (Backend §37).

See:
  Frontend-Design-Documentation.md §6.6 / §10.2 / §11.4
  Backend-Architecture-Documentation.md §37–38, §45
  roadmap Phase 11 §APIs
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.ask import AskCitationItem, AskSourceItem

ScopeTypeField = Literal["current_document", "selected_documents", "knowledge_base"]


# ── Requests ──────────────────────────────────────────────────────────────────

class ChatScopePayload(BaseModel):
    """The FE §6.6 scope selector, mirrored field-for-field."""

    type: ScopeTypeField = Field(
        description="current_document | selected_documents | knowledge_base."
    )
    document_ids: list[str] = Field(
        default_factory=list,
        description=(
            "In-scope document IDs (required for current_document/"
            "selected_documents; every ID must be currently accessible — "
            "violations are a 403, never a broadened scope)."
        ),
    )

    @field_validator("document_ids", mode="before")
    @classmethod
    def _normalize_ids(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(x) for x in v if x]
        return v


class ChatMessageRequest(BaseModel):
    """POST /chat/conversations/{id}/messages request body."""

    content: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4000,
            description="The user's message (the ORIGINAL words — the only "
                        "text answered or persisted).",
            examples=["What is the approval process?"],
        ),
    ]
    scope: ChatScopePayload | None = Field(
        default=None,
        description=(
            "Optional per-message scope override.  When the type (or the "
            "selected document set) differs from the conversation's current "
            "scope, the change is applied AND recorded as a visible SYSTEM "
            "marker message (Backend §46 rule 18)."
        ),
    )


class CreateConversationRequest(BaseModel):
    """POST /chat/conversations — lazy creation REQUIRES the first message.

    Empty conversations are never persisted (DB §20 lifecycle rule), so the
    first message is mandatory: creating a conversation without one would
    either accumulate clutter rows or need awkward deferred-persistence
    semantics.  The response is the SSE stream of the first answer.
    """

    message: ChatMessageRequest = Field(description="The first user message.")
    title: Annotated[
        str | None,
        Field(default=None, max_length=200, description="Optional title; auto-generated from the first message when omitted."),
    ] = None


class MessageFeedbackRequest(BaseModel):
    """POST /chat/messages/{id}/feedback — one rating per user per message."""

    rating: int = Field(ge=-1, le=1, description="+1 (positive) or -1 (negative).")
    comment: Annotated[
        str | None,
        Field(default=None, max_length=2000, description="Optional reason (typically captured on negative feedback)."),
    ] = None

    @field_validator("rating")
    @classmethod
    def _rating_in_set(cls, v: int) -> int:
        if v not in (-1, 1):
            raise ValueError("rating must be -1 or 1.")
        return v


# ── Responses ─────────────────────────────────────────────────────────────────

class ConversationSummary(BaseModel):
    """One row of the conversation list (recency-ordered)."""

    id: str
    title: str | None = Field(description="Auto-generated from the first message when not user-set.")
    scope_type: ScopeTypeField
    message_count: int = Field(description="Total messages (including SYSTEM markers).")
    created_at: datetime
    updated_at: datetime = Field(description="Recency cursor — bumped on every new message.")


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class MessageCitationItem(AskCitationItem):
    """Citation embedded in a persisted message payload (same contract)."""


class ChatMessageItem(BaseModel):
    """One persisted message in the conversation-detail payload."""

    id: str
    role: Literal["USER", "ASSISTANT", "SYSTEM"]
    content: str
    created_at: datetime
    # ASSISTANT-only fields (null otherwise — DB §21)
    model: str | None = None
    groundedness: Literal["grounded", "partial", "ungrounded"] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieval_ms: int | None = None
    latency_ms: int | None = None
    stopped: bool = Field(default=False, description="True when generation was frozen mid-answer by the stop control.")
    citations: list[MessageCitationItem] = Field(default_factory=list)
    my_feedback: Literal[-1, 1] | None = Field(
        default=None,
        description="The requesting user's existing rating for this message (null = none).",
    )


class ConversationDetailResponse(BaseModel):
    conversation: ConversationSummary
    messages: list[ChatMessageItem]
    limit: int
    offset: int
    total_messages: int


class ScopeInfo(BaseModel):
    type: ScopeTypeField
    document_ids: list[str] = Field(default_factory=list)


class StopResponse(BaseModel):
    message_id: str
    stopped: bool = Field(description="True when the stop flag was set (fail-open False = Redis unavailable).")


class MessageFeedbackResponse(BaseModel):
    message_id: str
    rating: int
    comment: str | None = None


# ── SSE payloads ──────────────────────────────────────────────────────────────

class ChatStartPayload(BaseModel):
    """The ``start`` SSE event — arrives before any token.

    Carries the pre-allocated ASSISTANT message id so the client can address
    ``POST /chat/messages/{id}/stop`` at the in-flight answer before any
    token (and therefore any persisted id) exists.
    """

    conversation_id: str = Field(description="Owning conversation UUID.")
    user_message_id: str = Field(description="The persisted USER message UUID.")
    assistant_message_id: str = Field(description="Pre-allocated ASSISTANT message UUID (stop-flag target).")
    scope: ScopeInfo = Field(description="The scope this turn runs under (post-override).")


class ChatDonePayload(BaseModel):
    """The terminal ``done`` SSE event (Backend §37: messageId + groundedness)."""

    message_id: str | None = Field(
        default=None,
        description="UUID of the persisted ASSISTANT message (null when a stop fired before any token).",
    )
    groundedness: Literal["grounded", "partial", "ungrounded"]
    stopped: bool = Field(default=False, description="True when the answer was frozen mid-generation by the stop control.")
    answer: str = Field(
        default="",
        description="The FINAL post-validation text — render THIS, not the raw token stream.",
    )
    user_message_id: str | None = Field(default=None, description="The USER message this answer answers.")
    citations: list[AskCitationItem] = Field(default_factory=list)
    sources: list[AskSourceItem] = Field(default_factory=list)
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stripped_claims: int = 0
    entailment_checks: int = 0
    injection_attempt: bool = Field(
        default=False,
        description="Phase 16: canary sentinel fired — filtered content notice for the FE.",
    )
    latency_ms: dict[str, Any] = Field(default_factory=dict)
