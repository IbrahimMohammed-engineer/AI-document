"""
Conversation repository — all data access for ``conversations`` +
``conversation_documents`` (Phase 11).

Tenant + ownership scoping: conversations are PRIVATE to the creating user
in V1 (DB §20), so every read takes BOTH ``organization_id`` AND ``user_id``
— the function signature is the tenancy guard (Backend §9 repository
discipline).  Soft-deleted ("archived") conversations are excluded from
every UI-facing read.

Scope-document writes are DIFF-based: unchecking a document sets
``removed_at`` (the row stays — scope changes are recorded, never silently
mutated; DB §21), re-checking resets it, new selections insert fresh rows.

See:
  Database-Architecture-Design-Documentation.md §20–21
  roadmap Phase 11 steps 1–2
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, update

from app.models.conversation import Conversation, ConversationDocument
from app.models.message import Message
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


class ConversationRepository(TenantScopedRepository[Conversation]):
    """Repository for conversations + their scope-document records."""

    model = Conversation

    # ── Creation (lazy — only ever called with a first message in hand) ──────

    async def create(
        self,
        *,
        organization_id: str,
        user_id: str,
        title: str | None,
        scope_type: str,
    ) -> Conversation:
        conversation = Conversation(
            organization_id=organization_id,
            user_id=user_id,
            title=title,
            scope_type=scope_type,
        )
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    # ── Reads (org + user scoped; soft-deleted excluded) ─────────────────────

    async def get_for_user(
        self, conversation_id: str, organization_id: str, user_id: str
    ) -> Conversation | None:
        """Fetch one conversation scoped to org + owner; archived excluded."""
        result = await self._session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == organization_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        organization_id: str,
        user_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Conversation]:
        """Recency-ordered page of the user's conversations (DB §27 index).

        The dominant read (FE §6.6 conversation list) — ordered by
        ``updated_at DESC`` so the conversation with the newest message is
        always on top.
        """
        result = await self._session.execute(
            select(Conversation)
            .where(
                self._org_filter(organization_id),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, organization_id: str, user_id: str) -> int:
        result = await self._session.execute(
            select(func.count(Conversation.id)).where(
                self._org_filter(organization_id),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return int(result.scalar_one())

    # ── Writes ────────────────────────────────────────────────────────────────

    async def bump_updated_at(self, conversation_id: str) -> None:
        """Bump the recency cursor (every new message — Backend Flow 4 step 14).

        A direct UPDATE (not ORM attribute mutation) so it composes safely
        inside either the caller's transaction or its own commit.
        """
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=func.now())
        )

    async def soft_delete(self, conversation: Conversation) -> Conversation:
        """Archive — history is retained but excluded from every UI read."""
        conversation.deleted_at = datetime.now(timezone.utc)
        await self._session.flush()
        return conversation

    # ── Scope documents (conversation_documents; diff-based writes) ──────────

    async def active_document_ids(self, conversation_id: str) -> list[str]:
        """Currently-in-scope document IDs (``removed_at IS NULL``)."""
        result = await self._session.execute(
            select(ConversationDocument.document_id).where(
                ConversationDocument.conversation_id == conversation_id,
                ConversationDocument.removed_at.is_(None),
            )
        )
        return [row for row in result.scalars().all()]

    async def set_scope_documents(
        self, conversation_id: str, document_ids: Sequence[str]
    ) -> None:
        """Replace the active scope with the given document set (diff-recorded).

        - newly selected documents      → new rows (added_at = now)
        - documents no longer selected  → removed_at = now (row RETAINED)
        - documents re-selected later   → removed_at reset, added_at bumped

        The caller must commit (or be inside a transaction that will).
        """
        desired = list(dict.fromkeys(document_ids))  # dedupe, keep order
        result = await self._session.execute(
            select(ConversationDocument).where(
                ConversationDocument.conversation_id == conversation_id
            )
        )
        existing = {row.document_id: row for row in result.scalars().all()}

        now = func.now()
        for document_id in desired:
            row = existing.get(document_id)
            if row is None:
                self._session.add(
                    ConversationDocument(
                        conversation_id=conversation_id,
                        document_id=document_id,
                    )
                )
            elif row.removed_at is not None:
                row.removed_at = None
                row.added_at = now

        for document_id, row in existing.items():
            if document_id not in desired and row.removed_at is None:
                row.removed_at = now
        await self._session.flush()

    # ── Message counts (conversation detail metadata) ─────────────────────────

    async def count_messages(self, conversation_id: str) -> int:
        result = await self._session.execute(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id
            )
        )
        return int(result.scalar_one())
