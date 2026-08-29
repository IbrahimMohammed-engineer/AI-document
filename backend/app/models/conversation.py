"""
SQLAlchemy ORM models for the conversation domain (Phase 11).

Tables:
  - Conversation           — the chat container; private to the creating user
                             in V1, scoped via ``scope_type`` +
                             ``conversation_documents`` (DB §20)
  - ConversationDocument   — the retrieval-scope record; ``removed_at`` (not
                             row deletion) records scope shrinkage so the
                             exact scope behind any historical message is
                             reconstructable (DB §21)
  - MessageFeedback        — one ±1 rating (+ optional comment) per user per
                             message; UNIQUE (message_id, user_id), a
                             resubmission upserts (DB §21)

See:
  Database-Architecture-Design-Documentation.md §20–21
  Backend-Architecture-Documentation.md §38 (Conversation Management)
  roadmap Phase 11 steps 1–2, 10–12
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.message import Message
    from app.models.user import User


# ── Conversation ──────────────────────────────────────────────────────────────

class Conversation(Base):
    """One chat conversation (DB §20).

    Lifecycle: created lazily on the FIRST message send — an empty
    conversation is never persisted (avoiding clutter in the conversation
    list).  ``updated_at`` is bumped on every new message and drives the
    conversation list's recency ordering.  ``deleted_at`` soft-deletes
    ("archive") — history remains queryable by ops, never by the UI.

    ``scope_type`` mirrors the frontend's scope selector exactly; a
    mid-conversation change is recorded BOTH here (current setting) and as
    a synthetic SYSTEM-role message marker (Backend §46 rule 18) so the
    scope active for each historical message stays answerable.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('current_document','selected_documents','knowledge_base')",
            name="ck_conversations_scope_type",
        ),
        Index(
            "ix_conversations_org_user_updated",
            "organization_id",
            "user_id",
            text("updated_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Owner — conversations are private to the creating user in V1",
    )
    title: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Auto-generated from the first message when not user-set",
    )
    scope_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="knowledge_base",
        server_default="knowledge_base",
        comment="current_document | selected_documents | knowledge_base",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="Bumped on every new message — recency ordering",
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft delete — archive conversation",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    scope_documents: Mapped[list[ConversationDocument]] = relationship(
        "ConversationDocument",
        back_populates="conversation",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="conversation",
        lazy="noload",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:
        return (
            f"<Conversation id={self.id!r} org={self.organization_id!r} "
            f"user={self.user_id!r} scope={self.scope_type!r}>"
        )


# ── ConversationDocument ──────────────────────────────────────────────────────

class ConversationDocument(Base):
    """One document in a conversation's retrieval scope (DB §21).

    Scope changes are RECORDED, never silently mutated: unchecking a
    document mid-conversation sets ``removed_at`` (the row stays), so the
    scope at the time of each message can always be reconstructed —
    essential for user trust ("this answer used these 2 documents") and
    retrieval-quality QA.
    """

    __tablename__ = "conversation_documents"
    __table_args__ = (
        Index("ix_conversation_documents_document_id", "document_id"),
    )

    conversation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set (not deleted) when a document is unchecked mid-conversation",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    conversation: Mapped[Conversation] = relationship(
        "Conversation",
        back_populates="scope_documents",
        lazy="noload",
    )
    document: Mapped[Document] = relationship(
        "Document", foreign_keys=[document_id], lazy="noload"
    )

    @property
    def is_active(self) -> bool:
        return self.removed_at is None

    def __repr__(self) -> str:
        return (
            f"<ConversationDocument conversation={self.conversation_id!r} "
            f"document={self.document_id!r} active={self.is_active}>"
        )


# ── MessageFeedback ───────────────────────────────────────────────────────────

class MessageFeedback(Base):
    """One rating per user per message (DB §21).

    Kept separate from ``messages`` because feedback has its own actor and
    timestamp, independent of the message's author.  UNIQUE
    ``(message_id, user_id)`` — a resubmission upserts the existing row
    rather than inserting a duplicate.
    """

    __tablename__ = "message_feedback"
    __table_args__ = (
        CheckConstraint("rating IN (-1, 1)", name="ck_message_feedback_rating"),
        # one rating per user per message — resubmission upserts (DB §21);
        # named constraint = the ON CONFLICT target for the upsert path
        UniqueConstraint(
            "message_id",
            "user_id",
            name="uq_message_feedback_message_user",
        ),
        Index("ix_message_feedback_message_id", "message_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="-1 (negative) / 1 (positive)",
    )
    comment: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional reason (captured on negative feedback)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    message: Mapped[Message] = relationship(
        "Message", foreign_keys=[message_id], lazy="noload"
    )
    user: Mapped[User] = relationship("User", foreign_keys=[user_id], lazy="noload")

    def __repr__(self) -> str:
        return (
            f"<MessageFeedback message={self.message_id!r} "
            f"user={self.user_id!r} rating={self.rating}>"
        )
