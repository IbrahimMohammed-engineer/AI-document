"""
Message-feedback repository — the ``message_feedback`` upsert path (Phase 11).

One rating per user per message (UNIQUE (message_id, user_id) — DB §21): a
resubmission UPSERTS the existing row rather than inserting a duplicate, so
changing a thumbs-up to thumbs-down (with or without a comment) is one
idempotent write.

See:
  Database-Architecture-Design-Documentation.md §21 (message_feedback)
  roadmap Phase 11 step 10
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import MessageFeedback

logger = logging.getLogger(__name__)


class MessageFeedbackRepository:
    """Repository for per-message user feedback (upsert semantics)."""

    model = MessageFeedback

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        message_id: str,
        user_id: str,
        rating: int,
        comment: str | None = None,
        commit: bool = True,
    ) -> MessageFeedback:
        """Insert or update the caller's rating for one message (DB §21).

        ON CONFLICT (message_id, user_id) DO UPDATE — a resubmission replaces
        both rating and comment atomically.  The instance is re-SELECTed
        after the write so an upsert in a session that already holds the
        row's identity returns the UPDATED state (never a stale
        identity-map copy).
        """
        stmt = (
            pg_insert(MessageFeedback)
            .values(
                message_id=message_id,
                user_id=user_id,
                rating=rating,
                comment=comment,
            )
            .on_conflict_do_update(
                constraint="uq_message_feedback_message_user",
                set_={"rating": rating, "comment": comment},
            )
            .returning(MessageFeedback.id)
        )
        result = await self._session.execute(stmt)
        feedback_id = result.scalar_one()
        await self._session.flush()

        feedback = await self._session.get(MessageFeedback, feedback_id)
        assert feedback is not None
        await self._session.refresh(feedback)
        if commit:
            await self._session.commit()
        logger.info(
            "MessageFeedback: user=%s rated message=%s rating=%+d",
            user_id, message_id, rating,
        )
        return feedback

    async def get_for_user(
        self, message_id: str, user_id: str
    ) -> MessageFeedback | None:
        """The caller's existing rating for one message (UI state restore)."""
        result = await self._session.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user_messages(
        self, message_ids: list[str], user_id: str
    ) -> dict[str, MessageFeedback]:
        """The caller's feedback for a page of messages (1 query, grouped)."""
        if not message_ids:
            return {}
        result = await self._session.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id.in_(message_ids),
                MessageFeedback.user_id == user_id,
            )
        )
        return {fb.message_id: fb for fb in result.scalars().all()}
