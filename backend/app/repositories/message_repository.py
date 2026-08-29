"""
Message repository — all data access for the ``messages`` + ``citations`` tables.

Phase 10 scope: the ASSISTANT write path — persisting the generated answer
and its resolved citations in ONE atomic transaction (Backend §50; roadmap
Phase 10 step 6).  A message with citation markers in its text but no
citations rows (or vice versa) is structurally impossible: both inserts are
flushed and committed together, and any failure rolls the whole write back.

Phase 11 scope: the conversation write/read paths — the USER message (its
own short transaction BEFORE retrieval begins — questions survive generation
failures; Backend Flow 4 step 17), SYSTEM scope-change markers, the
conversation-history read (messages + citations in two queries), and the
org-scoped single-message lookup that backs stop/feedback.

Tenant note (DB §21/§22 design): messages and citations carry no
``organization_id`` column of their own — tenancy flows through the
conversation (Phase 11) and the cited document FKs.  The Phase 10 write path
is driven by the authenticated request's pipeline output, whose retrieval
was already permission-scoped; cited ``quoted_text`` is therefore
permission-checked by construction (roadmap Phase 10 security note).

See:
  Backend-Architecture-Documentation.md §50 (Transaction Boundaries)
  Database-Architecture-Design-Documentation.md §21–22
  roadmap Phase 10 steps 3, 6; Phase 11 steps 3, 9
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Citation, Message
from app.rag.citations import ResolvedCitation

logger = logging.getLogger(__name__)


class MessageRepository:
    """Repository for messages + their citations."""

    model = Message

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_assistant_message_with_citations(
        self,
        *,
        content: str,
        groundedness: str,
        citations: Sequence[ResolvedCitation],
        conversation_id: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retrieval_ms: int | None = None,
        latency_ms: int | None = None,
        metadata: dict | None = None,
        message_id: str | None = None,
        commit: bool = True,
    ) -> Message:
        """Persist one ASSISTANT message and ALL its citations atomically.

        ONE transaction (Backend §50): the message row and every citation
        row flush together and commit together — a mid-write failure leaves
        NEITHER behind.  Citation rows are built here from the resolved
        citation objects so the mapping from RAG output to columns lives in
        exactly one place.

        Args:
            content:          The FINAL validated answer text (post-strip).
            groundedness:     grounded | partial | ungrounded.
            citations:        Resolved citations (rag/citations.py output).
            conversation_id:  None for standalone /ask answers; set for
                              conversation chat (Phase 11).
            metadata:         Generation metadata (e.g. {"stopped": true}).
            message_id:       Pre-allocated UUID (Phase 11: the chat stream's
                              ``start`` event announces this id so the stop
                              control can address the in-flight answer).
            commit:           When False the caller owns the transaction
                              (used when the write joins a larger unit of
                              work).  Default True — the Phase 10 path.
        """
        message = Message(
            conversation_id=conversation_id,
            role="ASSISTANT",
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            retrieval_ms=retrieval_ms,
            latency_ms=latency_ms,
            groundedness=groundedness,
            metadata_=metadata or None,
        )
        if message_id is not None:
            message.id = message_id
        self._session.add(message)
        await self._session.flush()  # assign message.id before citation FKs

        for citation in citations:
            self._session.add(
                Citation(
                    message_id=message.id,
                    citation_index=citation.index,
                    document_id=citation.document_id,
                    document_version_id=citation.document_version_id,
                    chunk_id=citation.chunk_id,
                    page_id=citation.page_id,
                    page_number=citation.page_number,
                    section=citation.section,
                    quoted_text=citation.quoted.text,
                    char_start=citation.quoted.char_start,
                    char_end=citation.quoted.char_end,
                    relevance_score=(
                        Decimal(str(round(citation.relevance, 5)))
                        if citation.relevance is not None
                        else None
                    ),
                )
            )
        await self._session.flush()

        if commit:
            await self._session.commit()
            logger.info(
                "MessageRepository: assistant message %s persisted with %d "
                "citation(s) (groundedness=%s conversation=%s)",
                message.id, len(citations), groundedness, conversation_id,
            )
        return message

    # ── Phase 11: conversation write + read paths ─────────────────────────────

    async def save_user_message(
        self,
        *,
        conversation_id: str,
        content: str,
        commit: bool = True,
    ) -> Message:
        """Persist the USER message in its OWN short transaction.

        Ordering property (Backend Flow 4 step 17): the question is committed
        BEFORE retrieval/generation begins, so a generation failure can never
        lose it — the client's retry affordance re-attaches to a durable row.
        """
        message = Message(
            conversation_id=conversation_id,
            role="USER",
            content=content,
        )
        self._session.add(message)
        await self._session.flush()
        if commit:
            await self._session.commit()
        return message

    async def save_system_message(
        self,
        *,
        conversation_id: str,
        content: str,
        commit: bool = True,
    ) -> Message:
        """Persist a SYSTEM marker (scope changes — Backend §46 rule 18).

        SYSTEM messages are visible history, never sent to the LLM as
        assistant output; they make "the scope changed here" reconstructable
        in the transcript.
        """
        message = Message(
            conversation_id=conversation_id,
            role="SYSTEM",
            content=content,
        )
        self._session.add(message)
        await self._session.flush()
        if commit:
            await self._session.commit()
        return message

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Message]:
        """Ordered (oldest → newest) message page for one conversation.

        Citations are NOT joined here — the dominant read renders badges from
        the denormalized columns, and the companion
        :meth:`citations_for_messages` fetches them for the page in ONE extra
        query (avoids the per-message lazy-load N+1 on large transcripts).
        """
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def citations_for_messages(
        self, message_ids: Sequence[str]
    ) -> dict[str, list[Citation]]:
        """All citations for the given messages, grouped by message_id (1 query)."""
        if not message_ids:
            return {}
        result = await self._session.execute(
            select(Citation)
            .where(Citation.message_id.in_(list(message_ids)))
            .order_by(Citation.message_id, Citation.citation_index)
        )
        grouped: dict[str, list[Citation]] = {}
        for citation in result.scalars().all():
            grouped.setdefault(citation.message_id, []).append(citation)
        return grouped

    async def get_chat_message_for_org(
        self, message_id: str, organization_id: str
    ) -> Message | None:
        """Fetch one CONVERSATION message scoped through its conversation's org.

        Used by stop + feedback (both target chat messages, never standalone
        /ask rows — those have no conversation and therefore no tenant path).
        Returns None when the message does not exist, is standalone, or
        belongs to another organization — callers treat all three as 404.
        """
        result = await self._session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id.isnot(None),
                Conversation.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_message_with_org(
        self, message_id: str
    ) -> tuple[Message, str] | None:
        """Fetch a conversation message + its owning organization_id.

        Distinguishes "not persisted yet" (an in-flight pre-allocated
        assistant id — the stop endpoint's normal target mid-stream) from
        "belongs to another organization" (a hard 404 — never set flags for
        foreign messages).  Returns None only when the id truly does not
        exist as a conversation message.
        """
        result = await self._session.execute(
            select(Message, Conversation.organization_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id.isnot(None),
            )
        )
        row = result.first()
        return (row[0], row[1]) if row else None

    async def list_history_pairs(
        self,
        conversation_id: str,
        *,
        max_messages: int,
    ) -> list[Message]:
        """The most recent USER/ASSISTANT messages (bounded LLM history window).

        SYSTEM markers are excluded (they are transcript metadata, not LLM
        context) as are standalone rows; ordered oldest → newest so the
        caller can hand the window straight to the rewriter/generator.
        """
        result = await self._session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role.in_(["USER", "ASSISTANT"]),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(max_messages)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return rows
