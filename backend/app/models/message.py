"""
SQLAlchemy ORM models for the messaging + citation domain (Phases 10–11).

Tables:
  - Message   — one chat message (USER / ASSISTANT / SYSTEM); assistant rows
                carry the generation metrics + groundedness outcome (DB §21)
  - Citation  — the explainability backbone: one resolved `[N]` reference per
                row, denormalized for zero-join rendering, FK-anchored to the
                exact chunk/page/version that supports the claim (DB §22)

Phase 11 note: ``Message.conversation_id`` now carries the foreign key to
``conversations`` (ON DELETE CASCADE — messages have no existence outside
their conversation).  The column STAYS NULLABLE: the standalone ``POST /ask``
pipeline is retained for the evaluation harness and persists with
``conversation_id NULL``.  ``metadata`` (JSONB) holds generation metadata
beyond the fixed metric columns — Phase 11 writes ``{"stopped": true}`` when
an answer was frozen mid-generation by the stop control.

See:
  Database-Architecture-Design-Documentation.md §21 (Message Model)
  Database-Architecture-Design-Documentation.md §22 (Citation Model)
  Backend-Architecture-Documentation.md §35–36 (citation generation/validation)
  Backend-Architecture-Documentation.md §50 (transaction boundaries)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.conversation import Conversation
    from app.models.document import Document, DocumentChunk, DocumentPage, DocumentVersion


# ── Message ───────────────────────────────────────────────────────────────────

class Message(Base):
    """One message in a conversation (or a standalone answer, Phase 10).

    Role-specific columns: ``model``/``prompt_tokens``/``completion_tokens``/
    ``retrieval_ms``/``latency_ms``/``groundedness`` are meaningful only for
    ASSISTANT rows and are CHECK-constrained NULL otherwise (DB §21 — a wide
    nullable table beats a join on the dominant "all messages for a
    conversation" read).

    See Database-Architecture-Design-Documentation.md §21.
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('USER','ASSISTANT','SYSTEM')",
            name="ck_messages_role",
        ),
        CheckConstraint(
            "groundedness IS NULL OR groundedness IN ('grounded','partial','ungrounded')",
            name="ck_messages_groundedness",
        ),
        CheckConstraint(
            "role = 'ASSISTANT' OR (model IS NULL AND prompt_tokens IS NULL "
            "AND completion_tokens IS NULL AND retrieval_ms IS NULL "
            "AND latency_ms IS NULL AND groundedness IS NULL)",
            name="ck_messages_assistant_only_metrics",
        ),
        CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0",
                        name="ck_messages_prompt_tokens"),
        CheckConstraint("completion_tokens IS NULL OR completion_tokens >= 0",
                        name="ck_messages_completion_tokens"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    # FK to conversations (Phase 11); NULL for standalone /ask answers.
    conversation_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
        comment="Owning conversation (NULL for standalone /ask answers)",
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retrieval_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    groundedness: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        server_default=text("'{}'"),
        key="metadata_",
        comment='Generation metadata (e.g. {"stopped": true} on stop-frozen answers)',
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    conversation: Mapped[Optional[Conversation]] = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
        lazy="noload",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    citations: Mapped[list[Citation]] = relationship(
        "Citation",
        back_populates="message",
        lazy="noload",
        cascade="all, delete-orphan",
        order_by="Citation.citation_index",
    )

    @property
    def is_assistant(self) -> bool:
        return self.role == "ASSISTANT"

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id!r} role={self.role!r} "
            f"conversation={self.conversation_id!r}>"
        )


# ── Citation ──────────────────────────────────────────────────────────────────

class Citation(Base):
    """One resolved inline reference — the explainability backbone (DB §22).

    Written ATOMICALLY with its assistant message (Backend §50); resolution
    comes from the backend's own ``SOURCE N → chunk`` context map, never from
    model-generated text (roadmap Phase 10 business rule 1 — citations are
    never fabricated).

    Denormalized columns (``document_id``, ``page_number``, ``section``)
    duplicate the normalized chain so badge/popover rendering needs zero
    joins; the real FKs keep the chain walkable in both directions and —
    because ``chunk_id`` is a foreign key — make it STRUCTURALLY impossible
    for a citation to reference content that was never actually retrieved.

    FK policy: RESTRICT to chunk/page/version/document — cited evidence
    cannot be silently deleted out from under a persisted answer.

    See Database-Architecture-Design-Documentation.md §22.
    """

    __tablename__ = "citations"
    __table_args__ = (
        CheckConstraint("citation_index >= 1", name="ck_citations_index"),
        CheckConstraint("page_number >= 1", name="ck_citations_page_number"),
        CheckConstraint("char_start IS NULL OR char_start >= 0",
                        name="ck_citations_char_start"),
        CheckConstraint("char_end IS NULL OR char_end >= 0",
                        name="ck_citations_char_end"),
        Index("ix_citations_message_id", "message_id"),
        Index("ix_citations_chunk_id", "chunk_id"),
        Index("ix_citations_document_id", "document_id"),
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
    citation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunk_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_chunks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    page_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_pages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quoted_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    relevance_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 5), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    message: Mapped[Message] = relationship(
        "Message",
        back_populates="citations",
        lazy="noload",
    )
    document: Mapped[Document] = relationship(
        "Document", foreign_keys=[document_id], lazy="noload"
    )
    version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion", foreign_keys=[document_version_id], lazy="noload"
    )
    chunk: Mapped[DocumentChunk] = relationship(
        "DocumentChunk", foreign_keys=[chunk_id], lazy="noload"
    )
    page: Mapped[DocumentPage] = relationship(
        "DocumentPage", foreign_keys=[page_id], lazy="noload"
    )

    def __repr__(self) -> str:
        return (
            f"<Citation message={self.message_id!r} index={self.citation_index} "
            f"chunk={self.chunk_id!r} page={self.page_number}>"
        )
