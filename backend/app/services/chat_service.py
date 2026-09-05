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
from app.services.conflict_service import ConflictNotice
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

    type: Literal["start", "token", "citation", "conflict_notice", "error", "done"]
    conversation_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    scope: "ScopeInfo | None" = None
    delta: str | None = None
    citation: AskCitation | None = None
    # Phase 13: deterministic inline conflict notices (§20) — structured
    # data from the DB query, never parsed from generated text.
    conflicts: "list[ConflictNotice] | None" = None
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

        # ── Phase 12: COMPARISON / CHANGE_DETECTION intent routing ─────────────
        # Attempt to resolve comparison targets deterministically (code-driven,
        # no LLM) from the conversation scope and query analysis (§14):
        #   - unresolved targets → persisted clarifying message (never a
        #     fabricated comparison, never a generic-RAG guess)
        #   - PENDING/PROCESSING → persisted "ask again shortly" notice; the
        #     SSE stream never blocks on the multi-minute worker job
        #   - COMPLETED → narrated, citation-backed answer persisted as a
        #     normal ASSISTANT Message + Citations (reused result, no recompute)
        try:
            from app.rag.query_analyzer import analyze_query
            from app.services.comparison_service import ComparisonService

            analysis = await analyze_query(context.user_message.content)
            if analysis.intent in ("COMPARISON", "CHANGE_DETECTION"):
                targets = await ComparisonService.resolve_comparison_targets_from_chat(
                    conversation=context.conversation,
                    analysis=analysis,
                    db=self._db,
                )
                if targets is None:
                    logger.info(
                        "ChatService: COMPARISON/CHANGE_DETECTION intent but "
                        "could not resolve targets deterministically — clarifying"
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "I can compare two document versions for you, but I "
                            "couldn't determine which ones you mean. Try selecting "
                            "exactly two documents in the conversation scope, or — "
                            "for the current document — ask about two specific "
                            "years, e.g. \"what changed between 2025 and 2026?\""
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                version_a_id, version_b_id = targets
                comparison, created = await ComparisonService.get_or_create_comparison(
                    user=user,
                    document_a_version_id=version_a_id,
                    document_b_version_id=version_b_id,
                    db=self._db,
                )

                if comparison.status != "COMPLETED":
                    logger.info(
                        "ChatService: comparison %s is %s — acknowledging without "
                        "blocking the stream (user re-asks once completed)",
                        comparison.id, comparison.status,
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "Comparing these versions now — this can take a "
                            "moment. Ask again shortly and I'll summarize the "
                            "detected changes."
                        ),
                        resolved_citations=[],
                        comparison_id=comparison.id,
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                # COMPLETED → serve the persisted result (reuse check above made
                # this free), narrate it (phrasing-only LLM call, §14), persist
                # as a normal ASSISTANT Message + Citations (§9.9 path 2).
                outcome = await self._narrate_comparison_outcome(user, context, comparison)
                for citation in outcome.citations:
                    yield ChatStreamEvent(type="citation", citation=citation)
                yield ChatStreamEvent(
                    type="done",
                    conversation_id=context.conversation.id,
                    outcome=outcome,
                )
                return

            # ── Phase 13: CONFLICT_DETECTION intent routing (§19) ────────────
            # Deterministic query → persisted, authorized conflicts → narrated.
            # The conversational layer orchestrates and narrates persisted
            # results — it never independently detects a new conflict.
            elif analysis.intent == "CONFLICT_DETECTION":
                from app.services.conflict_service import ConflictService

                accessible_ids: list[str] | None = context.resolved_document_ids
                if accessible_ids == []:
                    # Knowledge-base scope — the repository's private-source
                    # exclusion filter applies (per-user authorization at the
                    # same access-level rule every read surface uses).
                    accessible_ids = None
                conflicts_payload = await ConflictService.list_for_chat(
                    organization_id=user.organization_id,
                    accessible_document_ids=accessible_ids,
                    requesting_user=user,
                    topic_hint=getattr(analysis, "scope_hints", None),
                    db=self._db,
                )
                if not conflicts_payload:
                    # Deterministic, non-fabricated response — no LLM call
                    # needed to say "none found" (§19).
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "I didn't find any recorded conflicts in the "
                            "documents you have access to."
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return
                outcome = await self._narrate_conflict_outcome(
                    user, context, conflicts_payload
                )
                for citation in outcome.citations:
                    yield ChatStreamEvent(type="citation", citation=citation)
                yield ChatStreamEvent(
                    type="done",
                    conversation_id=context.conversation.id,
                    outcome=outcome,
                )
                return

            # ── Phase 14: SUMMARY intent routing (§5.12) ──────────────────────
            # Deterministic target resolution → get-or-create → narrate the
            # persisted (already-validated) summary.  Narration is a
            # deterministic re-render — no LLM call (§2.6 point 6).
            elif analysis.intent == "SUMMARY":
                from app.services.summary_service import SummaryService

                version_id = await SummaryService.resolve_summary_target_from_chat(
                    conversation=context.conversation,
                    analysis=analysis,
                    db=self._db,
                )
                if version_id is None:
                    logger.info(
                        "ChatService: SUMMARY intent but zero/multiple documents "
                        "in scope — clarifying"
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "I can summarize a document for you, but I need "
                            "exactly one document in scope. Select a document "
                            "in the conversation scope and ask again."
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                summary, _created = await SummaryService.get_or_create_summary(
                    user=user,
                    document_version_id=version_id,
                    db=self._db,
                )
                if summary.status != "COMPLETED":
                    logger.info(
                        "ChatService: summary %s is %s — acknowledging without "
                        "blocking the stream",
                        summary.id, summary.status,
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "Generating a summary of this document now — ask "
                            "again shortly and I'll walk you through it."
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                outcome = await self._narrate_summary_outcome(user, context, summary)
                for citation in outcome.citations:
                    yield ChatStreamEvent(type="citation", citation=citation)
                yield ChatStreamEvent(
                    type="done",
                    conversation_id=context.conversation.id,
                    outcome=outcome,
                )
                return

            # ── Phase 14: EXTRACTION intent routing (§5.12) ───────────────────
            # Chat-triggered extraction REUSES the latest COMPLETED run for
            # the resolved version (cost control, §2.6 point 5); narration is
            # an LLM call phrasing already-persisted items ("narrate, never
            # originate").
            elif analysis.intent == "EXTRACTION":
                from app.services.extraction_service import ExtractionService

                version_id = (
                    await ExtractionService.resolve_extraction_target_from_chat(
                        conversation=context.conversation,
                        analysis=analysis,
                        db=self._db,
                    )
                )
                if version_id is None:
                    logger.info(
                        "ChatService: EXTRACTION intent but zero/multiple documents "
                        "in scope — clarifying"
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "I can extract structured information (requirements, "
                            "risks, dates, parties) from a document, but I need "
                            "exactly one document in scope. Select a document "
                            "and ask again."
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                extraction, _created = (
                    await ExtractionService.get_or_create_run_from_chat(
                        user=user,
                        document_version_id=version_id,
                        db=self._db,
                    )
                )
                if extraction.status != "COMPLETED":
                    logger.info(
                        "ChatService: extraction run %s is %s — acknowledging",
                        extraction.id, extraction.status,
                    )
                    outcome = await self._persist_comparison_turn(
                        user,
                        context,
                        content=(
                            "Running the structured extraction now — ask again "
                            "shortly and I'll present the results."
                        ),
                        resolved_citations=[],
                    )
                    yield ChatStreamEvent(type="done", outcome=outcome)
                    return

                outcome = await self._narrate_extraction_outcome(
                    user, context, extraction
                )
                for citation in outcome.citations:
                    yield ChatStreamEvent(type="citation", citation=citation)
                yield ChatStreamEvent(
                    type="done",
                    conversation_id=context.conversation.id,
                    outcome=outcome,
                )
                return
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.exception(
                "ChatService: intent routing failed — falling through to RAG"
            )

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
            elif event.type == "conflict_notice":
                # Phase 13 (§20): forward the deterministic notices verbatim —
                # structured DB-derived data, never extracted from prose.
                yield ChatStreamEvent(
                    type="conflict_notice", conflicts=event.conflicts
                )
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

    # ── Phase 12: comparison-turn helpers (§14) ────────────────────────────────

    async def _persist_comparison_turn(
        self,
        user: User,
        context: "ChatMessageContext",
        *,
        content: str,
        resolved_citations: list[ResolvedCitation],
        comparison_id: str | None = None,
    ) -> AskOutcome:
        """Persist one comparison-path ASSISTANT turn and build its outcome.

        Reuses the exact atomic message+citations write path the RAG pipeline
        uses (MessageRepository + conversation recency bump, one transaction —
        Backend §50), then folds everything the FE needs into an AskOutcome.
        A persistence failure never corrupts the stream: the answer was
        already computed, and the write rolls back whole.
        """
        repo = MessageRepository(self._db)
        message_id: str | None = None
        try:
            message = await repo.save_assistant_message_with_citations(
                content=content,
                groundedness="grounded",
                citations=resolved_citations,
                conversation_id=context.conversation.id,
                message_id=context.assistant_message_id,
            )
            await ConversationRepository(self._db).bump_updated_at(context.conversation.id)
            await self._db.commit()
            message_id = message.id
        except Exception:
            logger.exception(
                "ChatService: comparison-turn persistence failed "
                "(conversation=%s) — neither row persisted",
                context.conversation.id,
            )
            await self._db.rollback()

        outcome = AskOutcome(
            question=context.user_message.content,
            groundedness="grounded",
            answer_text=content,
            final_answer=content,
            message_id=message_id,
        )
        if comparison_id is not None:
            outcome.comparison_id = comparison_id  # type: ignore[attr-defined]
        if resolved_citations:
            version_info = await AskService(self._db)._load_version_info(
                {c.document_version_id for c in resolved_citations}
            )
            outcome.citations = [
                AskService._to_ask_citation(c, version_info)
                for c in resolved_citations
            ]
        return outcome

    async def _narrate_comparison_outcome(
        self,
        user: User,
        context: "ChatMessageContext",
        comparison: object,
    ) -> AskOutcome:
        """Narrate a COMPLETED comparison and persist the answer + citations.

        The narration LLM call only phrases the already-classified
        comparison_changes rows (Backend §41); citations attach one per
        narrated change, pointing at the NEW side's chunk (falling back to
        the OLD side when the new chunk was deleted) — plan §9.9 path 2.
        """
        from app.infrastructure.llm import get_llm_provider
        from app.rag.citations import QuotedSpan
        from app.rag.comparison_narration import _fallback_narration, narrate_changes
        from app.repositories.document_comparison_repository import (
            DocumentComparisonRepository,
        )
        from app.services.comparison_service import ComparisonService

        repo = DocumentComparisonRepository(self._db)
        changes = await repo.list_changes(comparison.id)  # type: ignore[attr-defined]

        provider = get_llm_provider()
        if provider is not None:
            narration = await narrate_changes(changes, provider=provider)
        else:
            narration = _fallback_narration(changes)

        # ── Citations: one per change (bounded), new side preferred ────────
        resolved: list[ResolvedCitation] = []
        if changes:
            chunk_ids: list[str] = []
            for c in changes:
                for cid in (c.new_chunk_id, c.old_chunk_id):
                    if cid:
                        chunk_ids.append(cid)
            provenance = await ComparisonService.resolve_chunk_provenance(
                self._db, chunk_ids
            )
            for i, c in enumerate(changes[:10], start=1):
                chunk_id = c.new_chunk_id or c.old_chunk_id
                info = provenance.get(chunk_id or "")
                if not chunk_id or info is None:
                    continue  # chunk deleted — snapshot text still in narration
                snippet = (info["content"] or "")[:300]
                if not snippet:
                    snippet = (c.new_text or c.old_text or "")[:300]
                resolved.append(
                    ResolvedCitation(
                        index=i,
                        chunk_id=chunk_id,
                        document_id=info["document_id"],
                        document_version_id=info["document_version_id"],
                        document_name=info["document_name"],
                        page_id=info["page_id"],
                        page_number=info["page_number"],
                        section=info["section"],
                        relevance=1.0,
                        quoted=QuotedSpan(
                            text=snippet,
                            char_start=0,
                            char_end=len(snippet),
                        ),
                        claim_text=f"{c.change_type} in {c.section or 'document'}",
                    )
                )

        return await self._persist_comparison_turn(
            user,
            context,
            content=narration,
            resolved_citations=resolved,
            comparison_id=comparison.id,  # type: ignore[attr-defined]
        )

    # ── Phase 13: conflict-turn helpers (§19) ─────────────────────────────────

    async def _narrate_conflict_outcome(
        self,
        user: User,
        context: "ChatMessageContext",
        conflicts_payload: list[dict],
    ) -> AskOutcome:
        """Narrate already-persisted, already-authorized conflicts (§19).

        The narration LLM call only phrases the provided conflict list
        ("narrate, never originate" — the prompt forbids asserting any
        conflict not present in the input).  Citations attach one per
        statement referenced (bounded), pointing at the statement's chunk —
        the exact atomic message+citations persistence path the RAG pipeline
        uses (§50).
        """
        from app.infrastructure.llm import get_llm_provider
        from app.rag.citations import QuotedSpan
        from app.rag.conflict_narration import (
            fallback_conflict_narration,
            narrate_conflicts,
        )

        provider = get_llm_provider()
        if provider is not None:
            narration = await narrate_conflicts(conflicts_payload, provider=provider)
        else:
            narration = fallback_conflict_narration(conflicts_payload)

        # ── Citations: one per statement (bounded), real chunk provenance ──
        resolved: list[ResolvedCitation] = []
        index = 0
        for conflict in conflicts_payload:
            for statement in conflict.get("statements", []):
                if index >= 10:
                    break
                chunk_id = statement.get("chunk_id")
                if not chunk_id:
                    continue
                index += 1
                snippet = (statement.get("statement_text") or "")[:300]
                resolved.append(
                    ResolvedCitation(
                        index=index,
                        chunk_id=chunk_id,
                        document_id=statement["document_id"],
                        document_version_id=statement["document_version_id"],
                        document_name=statement.get("document_name") or "Unknown document",
                        page_id=statement["page_id"],
                        page_number=int(statement.get("page_number") or 1),
                        section=statement.get("section"),
                        relevance=1.0,
                        quoted=QuotedSpan(
                            text=snippet,
                            char_start=0,
                            char_end=len(snippet),
                        ),
                        claim_text=f"{conflict.get('topic', 'Conflict')} — {conflict.get('severity', '')}".strip(),
                    )
                )

        return await self._persist_comparison_turn(
            user,
            context,
            content=narration,
            resolved_citations=resolved,
        )

    # ── Phase 14: summary/extraction-turn helpers (§5.12) ─────────────────────

    async def _narrate_summary_outcome(
        self,
        user: User,
        context: "ChatMessageContext",
        summary: object,
    ) -> AskOutcome:
        """Narrate a COMPLETED summary — NO LLM call (§2.6 point 6).

        The persisted summary is already prose (validated, citation-tagged);
        chat narration is a deterministic re-render via narrate_summary, with
        the summary's own stored citations re-hydrated into ResolvedCitation
        objects (bounded) and persisted via the shared atomic turn writer.
        """
        from app.rag.citations import QuotedSpan
        from app.rag.summary_narration import narrate_summary

        narration = narrate_summary(summary)

        resolved: list[ResolvedCitation] = []
        payload = getattr(summary, "summary", None)
        if isinstance(payload, dict):
            for field_items in payload.values():
                if not isinstance(field_items, list):
                    continue
                for item in field_items:
                    if len(resolved) >= 10:
                        break
                    if not isinstance(item, dict):
                        continue
                    for stored in item.get("citations") or []:
                        if len(resolved) >= 10:
                            break
                        if not isinstance(stored, dict) or not stored.get("chunk_id"):
                            continue
                        quoted_text = str(stored.get("quoted_text") or "")
                        resolved.append(
                            ResolvedCitation(
                                index=len(resolved) + 1,
                                chunk_id=stored["chunk_id"],
                                document_id=stored.get("document_id") or "",
                                document_version_id=(
                                    stored.get("document_version_id") or ""
                                ),
                                document_name=(
                                    stored.get("document_name") or "Unknown document"
                                ),
                                page_id=stored.get("page_id") or "",
                                page_number=int(stored.get("page_number") or 1),
                                section=stored.get("section"),
                                relevance=float(stored.get("relevance") or 1.0),
                                quoted=QuotedSpan(
                                    text=quoted_text,
                                    char_start=int(stored.get("char_start") or 0),
                                    char_end=int(
                                        stored.get("char_end") or len(quoted_text)
                                    ),
                                ),
                                claim_text=str(item.get("text") or ""),
                            )
                        )

        return await self._persist_comparison_turn(
            user,
            context,
            content=narration,
            resolved_citations=resolved,
            comparison_id=getattr(summary, "id", None),
        )

    async def _narrate_extraction_outcome(
        self,
        user: User,
        context: "ChatMessageContext",
        extraction: object,
    ) -> AskOutcome:
        """Narrate a COMPLETED extraction run (§5.11/§5.12).

        One LLM call phrasing ONLY the persisted items ("narrate, never
        originate"), with the deterministic template fallback when no
        provider is configured.  Citations attach up to 10 items' stored
        provenance (bounded — mirrors the changes[:10]/statement[:10] bounds).
        """
        from app.infrastructure.llm import get_llm_provider
        from app.rag.citations import QuotedSpan
        from app.rag.extraction_narration import (
            _fallback_extraction_narration,
            narrate_extraction,
        )
        from app.repositories.document_extraction_repository import (
            DocumentExtractionRepository,
        )
        from app.services.comparison_service import ComparisonService

        repo = DocumentExtractionRepository(self._db)
        items = await repo.list_items(extraction.id)  # type: ignore[attr-defined]

        provider = get_llm_provider()
        if provider is not None:
            narration = await narrate_extraction(items, provider=provider)
        else:
            narration = _fallback_extraction_narration(items)

        resolved: list[ResolvedCitation] = []
        if items:
            provenance = await ComparisonService.resolve_chunk_provenance(
                self._db, [item.chunk_id for item in items]
            )
            for item in items[:10]:
                info = provenance.get(item.chunk_id)
                if info is None:
                    continue  # chunk deleted — snapshot text still in narration
                snippet = (item.quoted_text or "")[:300]
                resolved.append(
                    ResolvedCitation(
                        index=len(resolved) + 1,
                        chunk_id=item.chunk_id,
                        document_id=info["document_id"],
                        document_version_id=info["document_version_id"],
                        document_name=info["document_name"],
                        page_id=info["page_id"],
                        page_number=info["page_number"],
                        section=info["section"],
                        relevance=1.0,
                        quoted=QuotedSpan(
                            text=snippet,
                            char_start=item.char_start or 0,
                            char_end=item.char_end or len(snippet),
                        ),
                        claim_text=item.label,
                    )
                )

        return await self._persist_comparison_turn(
            user,
            context,
            content=narration,
            resolved_citations=resolved,
            comparison_id=getattr(extraction, "id", None),
        )

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
