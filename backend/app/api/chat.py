"""
Chat API router — persistent, scoped, streaming conversations (Phase 11).

Endpoints:
  GET    /chat/conversations                    — paginated, recency-ordered list
  POST   /chat/conversations                    — lazy creation + first message → SSE
  GET    /chat/conversations/{id}               — conversation + messages + citations
  DELETE /chat/conversations/{id}               — archive (soft delete)
  POST   /chat/conversations/{id}/messages      — ask → SSE stream
  POST   /chat/messages/{id}/stop               — explicit cancellation
  POST   /chat/messages/{id}/feedback           — ±1 rating (upsert)

SSE lifecycle (Backend §37): the stream opens only AFTER the synchronous
preparation phase succeeded (scope validated, USER message persisted, audit
written) — HTTP-level errors (401/403/404/429) arrive as normal status codes
BEFORE any event.  Once open, events are:

    event: start    data: {"conversation_id":…, "user_message_id":…,
                           "assistant_message_id":…, "scope":{…}}
    event: token    data: {"delta": "…"}           (zero or more)
    event: citation data: {…FE §12 contract…}      (×N — after validation)
    event: done     data: {"message_id":…, "groundedness":…, "stopped":…}
    event: error    data: {"code":…, "message":…}

Keep-alive comment lines (`: keep-alive`) are interleaved every ~15 s so
proxies/LBs never time out an apparently-idle connection during the long
retrieval/validation stages.  Client disconnect and the explicit stop flag
are both checked BETWEEN chunks — abandoned requests stop costing money,
and a stopped answer freezes its partial text as the final persisted answer.

Conversation reads are org+user scoped (private in V1 — DB §20); every route
is authentication-gated and chat routes additionally require the
``chat:create`` permission (live role re-check — never trusted from JWT).

See:
  Backend-Architecture-Documentation.md §37 (Streaming), §38 (Conversations),
  §45 (API), §63 Flow 4
  roadmap Phase 11 steps 3–6, 10–12
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import DbSession, require_permission
from app.core.config import get_settings
from app.core.exceptions import AppException, ExternalServiceError
from app.domain.permissions import PermissionKey
from app.infrastructure import stop_flags
from app.infrastructure.embeddings import EmbeddingProviderError
from app.models.message import Citation, Message
from app.models.user import User
from app.schemas.ask import AskSourceItem
from app.schemas.chat import (
    ChatDonePayload,
    ChatMessageItem,
    ChatMessageRequest,
    ChatStartPayload,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationSummary,
    CreateConversationRequest,
    MessageCitationItem,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    ScopeInfo,
    StopResponse,
)
from app.services.ask_service import AskCitation
from app.services.audit_logger import AuditAction, AuditLogger
from app.services.chat_service import (
    ChatMessageContext,
    ChatService,
    ChatStreamEvent,
    title_from_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering for SSE
}


# ── SSE formatting ────────────────────────────────────────────────────────────

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _citation_item(citation: AskCitation) -> MessageCitationItem:
    return MessageCitationItem(**citation.__dict__)


def _chat_event_sse(event: ChatStreamEvent) -> str:
    if event.type == "start":
        assert event.conversation_id and event.user_message_id
        assert event.assistant_message_id is not None
        assert event.scope is not None
        payload = ChatStartPayload(
            conversation_id=event.conversation_id,
            user_message_id=event.user_message_id,
            assistant_message_id=event.assistant_message_id,
            scope=ScopeInfo(
                type=event.scope.type,  # type: ignore[arg-type]
                document_ids=event.scope.document_ids,
            ),
        )
        return _sse("start", payload.model_dump(mode="json"))
    if event.type == "token":
        return _sse("token", {"delta": event.delta or ""})
    if event.type == "citation":
        assert event.citation is not None
        return _sse("citation", _citation_item(event.citation).model_dump(mode="json"))
    if event.type == "conflict_notice":
        # Phase 13 (§20): deterministic conflict notices — the FE renders the
        # ⚠ banner from this structured event, never from generated prose.
        conflicts = event.conflicts or []
        return _sse(
            "conflict_notice",
            {
                "conflicts": [
                    {
                        "conflict_id": c.conflict_id,
                        "topic": c.topic,
                        "severity": c.severity,
                    }
                    for c in conflicts
                ]
            },
        )
    if event.type == "error":
        return _sse(
            "error",
            {"code": event.error_code or "LLM_UNAVAILABLE",
             "message": event.error_message or "The AI service is unavailable."},
        )
    if event.type == "done":
        outcome = event.outcome
        assert outcome is not None
        payload = ChatDonePayload(
            message_id=outcome.message_id,
            groundedness=outcome.groundedness,
            stopped=outcome.stopped,
            answer=outcome.final_answer or outcome.message or "",
            user_message_id=event.user_message_id,
            citations=[_citation_item(c) for c in outcome.citations],
            sources=[AskSourceItem(**s.__dict__) for s in outcome.sources],
            model=outcome.model,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            stripped_claims=outcome.stripped_claims,
            entailment_checks=outcome.entailment_checks,
            latency_ms={
                "analyzer": outcome.timings.analyzer_ms,
                "rewrite": outcome.timings.rewrite_ms,
                "retrieval": outcome.timings.retrieval_ms,
                "context": outcome.timings.context_ms,
                "generation": outcome.timings.generation_ms,
                "citations": outcome.timings.citation_ms,
            },
        )
        return _sse("done", payload.model_dump(mode="json"))
    raise ValueError(f"Unknown chat stream event type: {event.type}")  # pragma: no cover


# ── Heartbeat wrapper (Backend §37 — keep-alive during long stages) ───────────

async def heartbeat_stream(
    events: AsyncIterator[ChatStreamEvent],
    interval: float,
) -> AsyncIterator[str]:
    """Yield formatted SSE frames, interleaving keep-alive comments.

    Events flow through a queue served by a producer task; the consumer
    waits with a timeout so a silent pipeline stage (retrieval, entailment)
    emits ``: keep-alive`` comments instead of a dead-air gap that proxies
    may treat as a stalled connection.  Cancelling the consumer (client
    disconnect) cancels the producer, which unwinds the whole pipeline.
    """
    queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    _DONE = object()

    async def _produce() -> None:
        try:
            async for event in events:
                await queue.put(event)
        except Exception as exc:  # propagate to the consumer
            await queue.put(exc)
        finally:
            await queue.put(_DONE)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield _chat_event_sse(item)
    finally:
        if not producer.done():
            producer.cancel()
        try:
            await producer
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cleanup
            pass


# ── Shared message-turn machinery ─────────────────────────────────────────────

async def _audit_question_asked(
    db, user: User, context: ChatMessageContext, request: Request
) -> None:
    """QUESTION_ASKED once the user message is persisted (Flow 4 step 19) —
    independent of whether generation ultimately succeeds."""
    try:
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.QUESTION_ASKED,
            resource_type="conversation",
            resource_id=context.conversation.id,
            metadata={
                "conversation_id": context.conversation.id,
                "user_message_id": context.user_message.id,
                "scope_type": context.conversation.scope_type,
                "scope_document_count": len(context.resolved_document_ids),
            },
            request=request,
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — audit must never break the turn
        logger.exception("QUESTION_ASKED audit write failed — continuing")


def _should_cancel_factory(
    request: Request, assistant_message_id: str
):
    """Cancellation check consulted BETWEEN chunks (Backend §37).

    Returns a stop-reason label ("disconnect" | "user") or None to continue.
    The stop flag is message-scoped and short-TTL (no cross-user reach).
    """

    async def _check() -> str | None:
        try:
            if await request.is_disconnected():
                return "disconnect"
        except Exception:  # noqa: BLE001 — transport quirks never stop generation
            pass
        if await stop_flags.is_stop_requested(assistant_message_id):
            return "user"
        return None

    return _check


async def _run_message_turn(
    *,
    db,
    user: User,
    request: Request,
    conversation_id: str,
    content: str,
    scope_type: str | None = None,
    document_ids: list[str] | None = None,
) -> StreamingResponse:
    """Shared body of POST /chat/conversations and POST …/messages.

    The synchronous preparation phase (scope re-validation, USER message
    persistence, audit) runs BEFORE the stream opens — its failures are
    ordinary HTTP errors.  Everything after the stream opens is SSE events.
    """
    settings = get_settings()
    service = ChatService(db)

    try:
        context = await service.prepare_message(
            user,
            conversation_id,
            content=content,
            scope_type=scope_type,
            document_ids=document_ids,
        )
    except AppException:
        raise  # 403/404/422 short-circuits — before any stream exists

    await _audit_question_asked(db, user, context, request)

    async def event_stream() -> AsyncIterator[str]:
        events = service.message_stream(
            user,
            context,
            should_cancel=_should_cancel_factory(request, context.assistant_message_id),
        )
        try:
            async for frame in heartbeat_stream(
                events, settings.sse_heartbeat_seconds
            ):
                yield frame
        except EmbeddingProviderError:
            logger.error("Chat: embedding provider error (user=%s)", user.id)
            yield _sse(
                "error",
                {"code": "EMBEDDING_UNAVAILABLE",
                 "message": "The search service is temporarily unavailable. "
                            "Please try again in a moment."},
            )
        except ExternalServiceError as exc:
            yield _sse("error", {"code": exc.error_code, "message": exc.message})
        except asyncio.CancelledError:
            # Client disconnect — the pipeline's cancellation path has the
            # partial answer frozen/persisted; just stop the stream quietly.
            raise
        except Exception:
            logger.exception("Chat: unhandled pipeline error (user=%s)", user.id)
            yield _sse(
                "error",
                {"code": "INTERNAL_ERROR",
                 "message": "An unexpected error occurred. Please try again "
                            "or contact support."},
            )

    return StreamingResponse(
        event_stream(), media_type=_MEDIA_TYPE, headers=_SSE_HEADERS
    )


# ── Conversation list ─────────────────────────────────────────────────────────

@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    summary="List the caller's conversations (recency-ordered page)",
)
async def list_conversations(
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ConversationListResponse:
    """Recency-ordered (updated_at DESC — DB §27), org+user-scoped,
    soft-deleted conversations excluded."""
    service = ChatService(db)
    conversations, total = await service.list_conversations(
        user, limit=limit, offset=offset
    )
    items: list[ConversationSummary] = []
    from app.repositories.conversation_repository import ConversationRepository

    repo = ConversationRepository(db)
    for conversation in conversations:
        items.append(
            ConversationSummary(
                id=conversation.id,
                title=conversation.title,
                scope_type=conversation.scope_type,  # type: ignore[arg-type]
                message_count=await repo.count_messages(conversation.id),
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )
    return ConversationListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


# ── Conversation detail ───────────────────────────────────────────────────────

@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="One conversation with its messages (citations + caller feedback)",
)
async def get_conversation(
    conversation_id: str,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ConversationDetailResponse:
    """Messages ordered oldest → newest with their citations embedded and the
    caller's feedback rating attached (FE §6.6 transcript restore)."""
    from app.repositories.message_feedback_repository import MessageFeedbackRepository
    from app.repositories.conversation_repository import ConversationRepository

    service = ChatService(db)
    conversation = await service.get_conversation(user, conversation_id)
    messages, citation_map = await service.conversation_messages(
        conversation, limit=limit, offset=offset
    )
    feedback_map = await MessageFeedbackRepository(db).list_for_user_messages(
        [m.id for m in messages], user.id
    )
    total = await ConversationRepository(db).count_messages(conversation.id)

    def _stopped_of(message: Message) -> bool:
        metadata = message.metadata_ or {}
        return bool(metadata.get("stopped"))

    items = [
        ChatMessageItem(
            id=m.id,
            role=m.role,  # type: ignore[arg-type]
            content=m.content,
            created_at=m.created_at,
            model=m.model,
            groundedness=m.groundedness,  # type: ignore[arg-type]
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            retrieval_ms=m.retrieval_ms,
            latency_ms=m.latency_ms,
            stopped=_stopped_of(m),
            citations=[
                _citation_from_row(row) for row in citation_map.get(m.id, [])
            ],
            my_feedback=(feedback_map[m.id].rating if m.id in feedback_map else None),  # type: ignore[arg-type]
        )
        for m in messages
    ]
    return ConversationDetailResponse(
        conversation=ConversationSummary(
            id=conversation.id,
            title=conversation.title,
            scope_type=conversation.scope_type,  # type: ignore[arg-type]
            message_count=total,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        ),
        messages=items,
        limit=limit,
        offset=offset,
        total_messages=total,
    )


def _citation_from_row(row: Citation) -> MessageCitationItem:
    """Map a persisted Citation row onto the FE §12 contract."""
    metadata = row.chunk.metadata if row.chunk is not None and row.chunk.metadata else {}
    return MessageCitationItem(
        index=row.citation_index,
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        document_name=metadata.get("document_name", ""),
        version_number=None,
        effective_date=None,
        page_id=row.page_id,
        page=row.page_number,
        section=row.section,
        text=row.quoted_text,
        char_start=row.char_start,
        char_end=row.char_end,
        context_before="",
        context_after="",
        relevance=float(row.relevance_score) if row.relevance_score is not None else 0.0,
    )


# ── Lazy creation + first message ─────────────────────────────────────────────

@router.post(
    "/conversations",
    summary="Create a conversation with its first message (SSE answer stream)",
    description=(
        "Lazy creation (DB §20): the conversation row is persisted BECAUSE a "
        "first message exists — empty conversations are never stored.  The "
        "response is the SSE stream of the first answer; the ``start`` event "
        "carries the new conversation's id.  Scope changes on later turns "
        "are recorded as SYSTEM marker messages."
    ),
)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
) -> StreamingResponse:
    service = ChatService(db)
    scope_type = body.message.scope.type if body.message.scope else "knowledge_base"
    document_ids = list(body.message.scope.document_ids) if body.message.scope else []

    # Resolve + authorize the scope BEFORE the row exists (403 pre-stream).
    _, resolved_ids = await service.resolve_scope(
        user, scope_type=scope_type, document_ids=document_ids
    )
    conversation = await service.create_conversation(
        user,
        title=body.title or title_from_message(body.message.content),
        scope_type=scope_type,
        document_ids=resolved_ids,
    )
    return await _run_message_turn(
        db=db,
        user=user,
        request=request,
        conversation_id=conversation.id,
        content=body.message.content,
    )


# ── Subsequent messages ───────────────────────────────────────────────────────

@router.post(
    "/conversations/{conversation_id}/messages",
    summary="Ask a question in a conversation (SSE stream)",
    description=(
        "Flow 4 (Backend §63): scope re-validated against CURRENT permissions "
        "→ USER message persisted BEFORE retrieval → streamed generation with "
        "stop/disconnect checks between chunks → citation events after "
        "validation → atomic ASSISTANT+citations persistence → done.  A "
        "mid-conversation scope override is applied and recorded as a visible "
        "SYSTEM marker; a scope referencing a now-inaccessible document is a "
        "403 short-circuit, never a broader fallback."
    ),
)
async def post_message(
    conversation_id: str,
    body: ChatMessageRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
) -> StreamingResponse:
    return await _run_message_turn(
        db=db,
        user=user,
        request=request,
        conversation_id=conversation_id,
        content=body.content,
        scope_type=body.scope.type if body.scope else None,
        document_ids=list(body.scope.document_ids) if body.scope else None,
    )


# ── Archive ───────────────────────────────────────────────────────────────────

@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    response_model=None,
    summary="Archive a conversation (soft delete — DB §20)",
)
async def delete_conversation(
    conversation_id: str,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
) -> None:
    service = ChatService(db)
    await service.delete_conversation(user, conversation_id)


# ── Stop ──────────────────────────────────────────────────────────────────────

@router.post(
    "/messages/{message_id}/stop",
    response_model=StopResponse,
    summary="Request cancellation of an in-flight answer",
    description=(
        "Sets a short-TTL, message-scoped Redis flag (Backend §37) that the "
        "streaming generator checks between chunks; the partial text freezes "
        "as the final answer (metadata stopped=true) and still runs citation "
        "validation.  The target id is the pre-allocated assistant message "
        "id announced in the stream's `start` event — it may legitimately "
        "not be persisted yet (the row writes when generation finishes). "
        "Idempotent; flags for already-finished messages simply expire."
    ),
)
async def stop_message(
    message_id: str,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
) -> StopResponse:
    from uuid import UUID as UUIDType

    from app.core.exceptions import NotFoundError
    from app.repositories.message_repository import MessageRepository

    try:
        UUIDType(message_id)
    except ValueError:
        raise NotFoundError("Message not found.")

    # Tenant check for PERSISTED messages: a foreign org's message is a hard
    # 404 (never set flags for it).  An unknown id is an in-flight
    # pre-allocated assistant message — its id is an unguessable UUID that
    # only the owning stream's client ever received, so setting the flag is
    # inert for anyone else (roadmap Phase 11 security note).
    found = await MessageRepository(db).get_message_with_org(message_id)
    if found is not None:
        _message, org_id = found
        if org_id != user.organization_id:
            raise NotFoundError("Message not found.")
    stopped = await stop_flags.request_stop(message_id)
    return StopResponse(message_id=message_id, stopped=stopped)


# ── Feedback ──────────────────────────────────────────────────────────────────

@router.post(
    "/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
    summary="Rate an assistant answer (one rating per user per message)",
    description=(
        "±1 with an optional comment; a resubmission UPSERTS the existing "
        "row (DB §21 unique constraint) — changing a rating is idempotent."
    ),
)
async def rate_message(
    message_id: str,
    body: MessageFeedbackRequest,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.CHAT_CREATE)),
) -> MessageFeedbackResponse:
    from app.repositories.message_feedback_repository import MessageFeedbackRepository
    from app.repositories.message_repository import MessageRepository

    message = await MessageRepository(db).get_chat_message_for_org(
        message_id, user.organization_id
    )
    if message is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("Message not found.")
    feedback = await MessageFeedbackRepository(db).upsert(
        message_id=message_id,
        user_id=user.id,
        rating=body.rating,
        comment=body.comment,
    )
    return MessageFeedbackResponse(
        message_id=message_id, rating=feedback.rating, comment=feedback.comment
    )
