"""
Chat service — persistent, scoped, streaming conversations (Phase 11).

Wraps the validated Phase 9–10 RAG pipeline in the conversation model
(Backend §38 + Flow 4).  The ordering below is a TRUST property, not an
implementation detail:

    1. Scope re-validation against CURRENT permissions (Backend §46 rule 3)
       — a document removed from the user's access mid-conversation yields a
       403 short-circuit, never a broader fallback.
    2. Scope changes recorded as SYSTEM-role marker messages (Backend §46
       rule 18) — the scope active for each historical message is always
       reconstructable (DB §20–21).
    3. USER message persisted in its OWN short transaction BEFORE retrieval —
       questions survive generation failures (Backend Flow 4 step 17).
    4. The Phase 9–10 pipeline runs with cancellation checks between chunks
       (explicit stop / client disconnect — Backend §37); a cancelled answer
       freezes its partial text and STILL goes through citation validation.
    5. ASSISTANT message + citations persisted atomically AFTER validation,
       never mid-stream; conversation.updated_at bumps in the same
       transaction (Backend §50, Flow 4 step 14).

Conversations are created LAZILY — only ever with a first message in hand
(DB §20 lifecycle rule: empty conversations are never persisted).  Chat is
synchronous-within-one-streamed-request, never a background job (Backend §45
— latency-sensitive).

See:
  Backend-Architecture-Documentation.md §26 (RAG), §37 (Streaming),
  §38 (Conversation Management), §45/§46, §63 Flow 4
  roadmap Phase 11 steps 2–6, 11–12
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, NotFoundError
from app.domain.versioning import VersionScope
from app.infrastructure.llm import LLMMessage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.rag.citations import ResolvedCitation
from app.services.ask_service import AskCitation, AskOutcome, AskService
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository

logger = logging.getLogger(__name__)

# ── Scope vocabulary (mirrors FE §6.6's selector exactly) ─────────────────────

ScopeType = Literal["current_document", "selected_documents", "knowledge_base"]
SCOPE_TYPES: tuple[str, ...] = ("current_document", "selected_documents", "knowledge_base")

_SCOPE_LABELS = {
    "current_document": "Current document",
    "selected_documents": "Selected documents",
    "knowledge_base": "Entire knowledge base",
}


# ── Stream event model (chat SSE protocol — Backend §37) ─────────────────────

@dataclass
class ChatStreamEvent:
    """One SSE event in the chat response stream.

    ``type`` is one of:
      start    — {"conversationId","userMessageId","assistantMessageId"}:
                 emitted FIRST so the client can address a stop request to
                 the assistant message before any token exists
      token    — {"delta": "..."}                    (zero or more)
      citation — one per resolved citation, AFTER generation + validation —
                 never mid-stream (validity unknowable earlier, Backend §37)
      error    — {"code","message"}; the user's question stays persisted
      done     — terminal: {"messageId","groundedness","stopped","answer",
                 "citations","sources", ...}
    """

    type: Literal["start", "token", "citation", "error", "done"]
    conversation_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    scope: "ScopeInfo | None" = None
    delta: str | None = None
    citation: AskCitation | None = None
    outcome: AskOutcome | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ScopeInfo:
    """The scope a turn runs under (post-override) — rides the start event."""

    type: str
    document_ids: list[str] = field(default_factory=list)


@dataclass
class ChatMessageContext:
    """Everything the stream needs after the synchronous preparation phase."""

    conversation: Conversation
    user_message: Message
    scope: VersionScope
    history: list[LLMMessage]
    assistant_message_id: str
    scope_changed: bool = False
    resolved_document_ids: list[str] = field(default_factory=list)


# ── Service ───────────────────────────────────────────────────────────────────

class ChatService:
    """Owns conversations: creation, scopes, markers, message streaming."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._settings = get_settings()

    # ── Scope resolution + re-validation (every message) ─────────────────────

    async def resolve_scope(
        self,
        user: User,
        *,
        scope_type: str,
        document_ids: Sequence[str],
    ) -> tuple[VersionScope, list[str]]:
        """Resolve a scope selection into a VersionScope + accessible doc IDs.

        Re-validates EVERY document against LIVE permissions (Backend §46
        rule 3): the returned list is the intersection of the selection with
        what the user may currently access.  Any selected document that is
        no longer accessible raises 403 — the scope is never silently
        broadened or pruned (roadmap Phase 11 step 12; Backend §45).
        """
        if scope_type not in SCOPE_TYPES:
            raise NotFoundError("Unknown scope type.")

        if scope_type == "knowledge_base":
            return VersionScope.all_documents(), []

        if not document_ids:
            raise ForbiddenError(
                "The selected scope contains no accessible documents."
            )

        accessible = await self._accessible_document_ids(user, set(document_ids))
        missing = [doc_id for doc_id in document_ids if doc_id not in accessible]
        if missing:
            raise ForbiddenError(
                "One or more documents in the scope are no longer accessible."
            )
        ordered = [doc_id for doc_id in document_ids if doc_id in accessible]
        return VersionScope.for_documents(ordered), ordered

    async def _accessible_document_ids(
        self, user: User, document_ids: set[str]
    ) -> set[str]:
        """Document-level access check (same rules as §29 step 2).

        organization-level documents → every org member; private/restricted
        → owner only.  Soft-deleted/inactive documents are never accessible.
        """
        from sqlalchemy import select

        from app.models.document import Document

        result = await self._db.execute(
            select(Document).where(
                Document.id.in_(document_ids),
                Document.organization_id == user.organization_id,
                Document.deleted_at.is_(None),
                Document.status == "active",
            )
        )
        documents = list(result.scalars().all())
        accessible: set[str] = set()
        for doc in documents:
            if doc.access_level == "organization" or doc.owner_id == user.id:
                accessible.add(doc.id)
        return accessible

    # ── Conversation lifecycle (lazy; DB §20) ─────────────────────────────────

    async def create_conversation(
        self,
        user: User,
        *,
        title: str | None,
        scope_type: str,
        document_ids: Sequence[str],
    ) -> Conversation:
        """Persist a conversation — called ONLY with a first message pending.

        Empty conversations are never persisted (DB §20): the API surface
        requires the first message in the same request, so a bare row can
        never accumulate.
        """
        scope, resolved_ids = await self.resolve_scope(
            user, scope_type=scope_type, document_ids=document_ids
        )
        repo = ConversationRepository(self._db)
        conversation = await repo.create(
            organization_id=user.organization_id,
            user_id=user.id,
            title=title,
            scope_type=scope_type,
        )
        if scope_type != "knowledge_base":
            await repo.set_scope_documents(conversation.id, resolved_ids)
        await self._db.commit()
        logger.info(
            "ChatService: conversation %s created (org=%s user=%s scope=%s docs=%d)",
            conversation.id, user.organization_id, user.id, scope_type, len(resolved_ids),
        )
        return conversation

    async def get_conversation(self, user: User, conversation_id: str) -> Conversation:
        """Org+owner-scoped fetch (private in V1 — DB §20); 404 otherwise."""
        conversation = await ConversationRepository(self._db).get_for_user(
            conversation_id, user.organization_id, user.id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    async def list_conversations(
        self, user: User, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[Conversation], int]:
        """Recency-ordered page (DB §27 index) + total for pagination."""
        repo = ConversationRepository(self._db)
        conversations = await repo.list_for_user(
            user.organization_id, user.id, limit=limit, offset=offset
        )
        total = await repo.count_for_user(user.organization_id, user.id)
        return conversations, total

    async def delete_conversation(self, user: User, conversation_id: str) -> None:
        """Archive (soft delete — DB §20): hidden from the UI, not destroyed."""
        conversation = await self.get_conversation(user, conversation_id)
        await ConversationRepository(self._db).soft_delete(conversation)
        await self._db.commit()

    # ── Message preparation (Flow 4 steps 6–9 — synchronous, before stream) ──

    async def prepare_message(
        self,
        user: User,
        conversation_id: str,
        *,
        content: str,
        scope_type: str | None = None,
        document_ids: Sequence[str] | None = None,
    ) -> ChatMessageContext:
        """Validate scope, record changes, persist the USER message.

        Steps (Backend Flow 4):
          1. Load the conversation (org+user scoped).
          2. Per-message scope override → re-validate against CURRENT
             permissions; 403 short-circuit on any inaccessible document.
          3. A changed scope is applied AND recorded as a SYSTEM marker
             message (Backend §46 rule 18) — never a silent mutation.
          4. The USER message persists in its OWN short transaction BEFORE
             retrieval begins — a question is never lost to a generation
             failure; the audit QUESTION_ASKED rides the same moment
             (Flow 4 step 19).
          5. The assistant message ID is allocated NOW (before any token) so
             the stop control can address the in-flight generation.
        """
        conversation = await self.get_conversation(user, conversation_id)
        repo = ConversationRepository(self._db)

        # ── Scope override / re-validation ────────────────────────────────
        override_requested = scope_type is not None
        effective_type = scope_type or conversation.scope_type
        scope_changed = False

        if not override_requested:
            # Re-validate the EXISTING scope against CURRENT permissions
            # (Backend §46 rule 3 — access may have changed since the last
            # message).  A now-inaccessible document is a 403 short-circuit,
            # never a silently pruned or broadened scope.
            if effective_type == "knowledge_base":
                scope = VersionScope.all_documents()
                resolved_ids: list[str] = []
            else:
                active_ids = await repo.active_document_ids(conversation.id)
                accessible = await self._accessible_document_ids(
                    user, set(active_ids)
                )
                if set(active_ids) - accessible:
                    raise ForbiddenError(
                        "One or more documents in the scope are no longer "
                        "accessible."
                    )
                resolved_ids = list(active_ids)
                # An empty resolved scope flows through retrieval as zero
                # candidates → the honest ungrounded response (never a
                # broader search — Backend Flow 4 step 15).
                scope = VersionScope.for_documents(resolved_ids)
        else:
            scope, resolved_ids = await self.resolve_scope(
                user,
                scope_type=effective_type,
                document_ids=list(document_ids or []),
            )
            if effective_type == "knowledge_base":
                resolved_ids = []

            current_active = set(await repo.active_document_ids(conversation.id))
            scope_changed = effective_type != conversation.scope_type or (
                effective_type != "knowledge_base"
                and current_active != set(resolved_ids)
            )
            if scope_changed:
                conversation.scope_type = effective_type
                await repo.set_scope_documents(
                    conversation.id,
                    resolved_ids if effective_type != "knowledge_base" else [],
                )

        if scope_changed:
            label = _SCOPE_LABELS[effective_type]
            if effective_type == "knowledge_base":
                marker = f"Scope changed to {label}."
            else:
                marker = f"Scope changed to {label} ({len(resolved_ids)} document(s))."
            await MessageRepository(self._db).save_system_message(
                conversation_id=conversation.id, content=marker, commit=False
            )
            await self._db.commit()
            logger.info(
                "ChatService: scope change on conversation %s → %s (%d docs)",
                conversation.id, effective_type, len(resolved_ids),
            )

        # ── USER message: own short transaction, BEFORE retrieval ─────────
        message_repo = MessageRepository(self._db)
        user_message = await message_repo.save_user_message(
            conversation_id=conversation.id, content=content
        )
        await repo.bump_updated_at(conversation.id)
        await self._db.commit()

        # ── Bounded history window (Backend §28 — 2–3 turns) ──────────────
        history_rows = await message_repo.list_history_pairs(
            conversation.id,
            max_messages=self._settings.llm_history_turns * 2,
        )
        # The just-persisted USER message is the prompt — exclude it from
        # the history window handed to rewriter/generator.
        history = [
            LLMMessage(role=row.role.lower(), content=row.content)
            for row in history_rows
            if row.id != user_message.id
        ]

        return ChatMessageContext(
            conversation=conversation,
            user_message=user_message,
            scope=scope,
            history=history,
            assistant_message_id=_new_message_id(),
            scope_changed=scope_changed,
            resolved_document_ids=resolved_ids,
        )

    # ── The streamed turn (Flow 4 steps 8–18) ─────────────────────────────────

    async def message_stream(
        self,
        user: User,
        context: ChatMessageContext,
        *,
        should_cancel=None,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Run the pipeline for a prepared message and translate its events.

        The chat SSE protocol (Backend §37): ``start`` → ``token``* →
        ``citation``* (after validation) → ``done``; ``error`` on mid-stream
        failure (the question stays persisted — Flow 4 step 15).
        """
        yield ChatStreamEvent(
            type="start",
            conversation_id=context.conversation.id,
            user_message_id=context.user_message.id,
            assistant_message_id=context.assistant_message_id,
            scope=ScopeInfo(
                type=context.conversation.scope_type,
                document_ids=context.resolved_document_ids,
            ),
        )

        ask_service = AskService(self._db)
        scope_label = _SCOPE_LABELS[context.conversation.scope_type]

        async for event in ask_service.ask_stream(
            context.user_message.content,
            user,
            scope=context.scope,
            history=context.history,
            conversation_id=context.conversation.id,
            assistant_message_id=context.assistant_message_id,
            should_cancel=should_cancel,
        ):
            if event.type == "token":
                yield ChatStreamEvent(type="token", delta=event.text or "")
            elif event.type == "citation":
                pass  # citations arrive with the done payload AND as their own events below
            elif event.type == "done":
                outcome = event.outcome
                assert outcome is not None
                # One citation event per resolved citation — AFTER generation
                # + validation (Backend §37: never mid-stream).
                for citation in outcome.citations:
                    yield ChatStreamEvent(type="citation", citation=citation)
                logger.info(
                    "ChatService: conversation=%s user_message=%s assistant_message=%s "
                    "groundedness=%s scope=%s docs=%d stopped=%s citations=%d",
                    context.conversation.id, context.user_message.id,
                    outcome.message_id, outcome.groundedness, scope_label,
                    len(context.resolved_document_ids), outcome.stopped,
                    len(outcome.citations),
                )
                yield ChatStreamEvent(
                    type="done",
                    conversation_id=context.conversation.id,
                    outcome=outcome,
                )
            elif event.type == "error":
                yield ChatStreamEvent(
                    type="error",
                    error_code=event.error_code or "LLM_UNAVAILABLE",
                    error_message=event.error_message or "The AI service is unavailable.",
                )
            elif event.type == "sources":
                # The chat protocol folds sources into the done payload.
                continue

    # ── History read (conversation detail) ────────────────────────────────────

    async def conversation_messages(
        self,
        conversation: Conversation,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Message], dict[str, list[ResolvedCitation]]]:
        """One message page + its citations grouped by message (2 queries)."""
        repo = MessageRepository(self._db)
        messages = await repo.list_for_conversation(
            conversation.id, limit=limit, offset=offset
        )
        citation_map = await repo.citations_for_messages([m.id for m in messages])
        return messages, citation_map


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_message_id() -> str:
    """Allocate the assistant message UUID upfront (stop-flag target)."""
    import uuid

    return str(uuid.uuid4())


def title_from_message(content: str, max_length: int = 60) -> str:
    """Auto-title from the first message (DB §20) — first line, trimmed."""
    first_line = content.strip().splitlines()[0] if content.strip() else "New conversation"
    if len(first_line) > max_length:
        return first_line[: max_length - 1].rstrip() + "…"
    return first_line
