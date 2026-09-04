"""
SQLAlchemy ORM models for the conflict-detection domain (Phase 13).

Tables:
  - Conflict            — one detected contradiction (aggregate root): topic,
                          severity, lifecycle status, detection method
  - ConflictStatement   — N evidence statements per conflict (N >= 2); the
                          Citation provenance shape applied to a different
                          aggregate root (all provenance FKs NOT NULL +
                          ON DELETE RESTRICT — a conflict statement with no
                          real chunk evidence is not a valid statement)

Deliberate non-storage: a statement's version CURRENT/SUPERSEDED/SCHEDULED
state is NEVER persisted — it is computed live at every read via
``domain.versioning.classify_version_state`` (a version's state can change
after a conflict is recorded).  ``effective_date`` is a static denormalized
display value only.

No soft-delete columns on either table (DB §29 — immutable historical
record): a DISMISSED conflict is never deleted, only status-transitioned.

See:
  Database-Architecture-Design-Documentation.md §26
  PHASE-13-IMPLEMENTATION-PLAN.md §8, §9
"""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document, DocumentChunk, DocumentPage, DocumentVersion
    from app.models.organization import Organization
    from app.models.user import User


class Conflict(Base):
    """One detected contradiction across documents.

    Lifecycle (Backend §42 / DB §26 — both resolved states are terminal,
    a conflict is never automatically reopened):
        OPEN ──resolve(REVIEWED)──► REVIEWED
        OPEN ──resolve(DISMISSED)─► DISMISSED
    """

    __tablename__ = "conflicts"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('MAJOR','MODERATE','MINOR')",
            name="ck_conflicts_severity",
        ),
        CheckConstraint(
            "status IN ('OPEN','REVIEWED','DISMISSED')",
            name="ck_conflicts_status",
        ),
        CheckConstraint(
            "detection_method IN ('BACKGROUND_SCAN','COMPARISON_DERIVED','RETRIEVAL_TIME')",
            name="ck_conflicts_detection_method",
        ),
        # A conflict is resolved by a person at a point in time, or it is
        # open — stale resolver data on an OPEN row is structurally impossible.
        CheckConstraint(
            "(status = 'OPEN' AND resolved_by IS NULL AND resolved_at IS NULL) "
            "OR (status <> 'OPEN' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_conflicts_resolution_fields",
        ),
        Index("ix_conflicts_org_status", "organization_id", "status"),
        Index("ix_conflicts_org_detected", "organization_id", "detected_at"),
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
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="OPEN",
        server_default="OPEN",
    )
    detection_method: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="BACKGROUND_SCAN | COMPARISON_DERIVED | RETRIEVAL_TIME (read-only in V1)",
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="noload",
    )
    resolver: Mapped[Optional[User]] = relationship(
        "User",
        foreign_keys=[resolved_by],
        lazy="noload",
    )
    statements: Mapped[list[ConflictStatement]] = relationship(
        "ConflictStatement",
        back_populates="conflict",
        cascade="all, delete-orphan",
        order_by="ConflictStatement.created_at",
        lazy="noload",
    )

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def is_resolved(self) -> bool:
        return self.status in ("REVIEWED", "DISMISSED")

    def __repr__(self) -> str:
        return (
            f"<Conflict id={self.id!r} status={self.status!r} "
            f"severity={self.severity!r} topic={self.topic!r}>"
        )


class ConflictStatement(Base):
    """One evidence statement within a conflict.

    Provenance mirrors the Citation table's FK policy exactly (NOT NULL +
    RESTRICT everywhere): cited evidence cannot be silently deleted out
    from under a persisted conflict.  ``statement_text`` is a denormalized
    snapshot of the source chunk's content — the statement stays displayable
    even if the source chunk is later modified (same reasoning as
    comparison_changes.old_text/new_text).
    """

    __tablename__ = "conflict_statements"
    __table_args__ = (
        # The same chunk can never appear twice as a statement within one
        # conflict — structural backstop under the application-level dedup.
        UniqueConstraint(
            "conflict_id",
            "chunk_id",
            name="uq_conflict_statements_conflict_chunk",
        ),
        Index("ix_conflict_statements_conflict_id", "conflict_id"),
        Index("ix_conflict_statements_chunk_id", "chunk_id"),
        Index(
            "ix_conflict_statements_document_version_id",
            "document_version_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    conflict_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("conflicts.id", ondelete="CASCADE"),
        nullable=False,
    )
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
    statement_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Static denormalized display value (DB §26) — NEVER the source of the
    # live CURRENT/SUPERSEDED/SCHEDULED classification (§8, §12).
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    conflict: Mapped[Conflict] = relationship(
        "Conflict",
        back_populates="statements",
    )
    document: Mapped[Document] = relationship(
        "Document",
        foreign_keys=[document_id],
        lazy="noload",
    )
    version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        foreign_keys=[document_version_id],
        lazy="noload",
    )
    chunk: Mapped[DocumentChunk] = relationship(
        "DocumentChunk",
        foreign_keys=[chunk_id],
        lazy="noload",
    )
    page: Mapped[DocumentPage] = relationship(
        "DocumentPage",
        foreign_keys=[page_id],
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<ConflictStatement id={self.id!r} conflict={self.conflict_id!r} "
            f"chunk={self.chunk_id!r}>"
        )
