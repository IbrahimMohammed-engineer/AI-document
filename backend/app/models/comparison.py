"""
SQLAlchemy ORM models for document comparison domain.

Tables:
  - DocumentComparison  — one row per unique (org, versionA, versionB) pair
  - ComparisonChange    — individual change detected between two section versions

Lifecycle (Phase 12 — §9.1):
  PENDING → PROCESSING → COMPLETED
                │
                └───────→ FAILED

See:
  Database-Architecture-Design-Documentation.md §25
  PHASE-12-IMPLEMENTATION-PLAN.md §10 Task 1
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import DocumentChunk, DocumentVersion
    from app.models.organization import Organization
    from app.models.user import User


class DocumentComparison(Base):
    """One persisted result per unique (org, versionA, versionB) pair.

    Order-normalized at write time: document_a_version_id always carries the
    lexicographically-smaller (document_id, version_number) tuple so the unique
    constraint is stable regardless of which order the caller supplied the pair.

    Access-level re-checked on every read (not only at creation time) so a
    later ``access_level`` change on either source document correctly gates the
    comparison result.
    """

    __tablename__ = "document_comparisons"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')",
            name="ck_document_comparisons_status",
        ),
        CheckConstraint(
            "document_a_version_id <> document_b_version_id",
            name="ck_document_comparisons_distinct_versions",
        ),
        UniqueConstraint(
            "organization_id",
            "document_a_version_id",
            "document_b_version_id",
            name="uq_document_comparisons_pair",
        ),
        Index("ix_document_comparisons_org_status", "organization_id", "status"),
        Index("ix_document_comparisons_a", "document_a_version_id"),
        Index("ix_document_comparisons_b", "document_b_version_id"),
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
    document_a_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Lexicographically-smaller (document_id, version_number) side",
    )
    document_b_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Lexicographically-larger (document_id, version_number) side",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PENDING",
        server_default="PENDING",
    )
    summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="noload",
    )
    version_a: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        foreign_keys=[document_a_version_id],
        lazy="noload",
    )
    version_b: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        foreign_keys=[document_b_version_id],
        lazy="noload",
    )
    requested_by_user: Mapped[User] = relationship(
        "User",
        foreign_keys=[requested_by],
        lazy="noload",
    )
    changes: Mapped[list[ComparisonChange]] = relationship(
        "ComparisonChange",
        back_populates="comparison",
        cascade="all, delete-orphan",
        order_by="ComparisonChange.created_at",
        lazy="noload",
    )

    @property
    def is_complete(self) -> bool:
        return self.status == "COMPLETED"

    @property
    def is_failed(self) -> bool:
        return self.status == "FAILED"

    def __repr__(self) -> str:
        return (
            f"<DocumentComparison id={self.id!r} status={self.status!r} "
            f"a={self.document_a_version_id!r} b={self.document_b_version_id!r}>"
        )


class ComparisonChange(Base):
    """One detected difference between a section in version A and version B.

    ``UNCHANGED`` sections are NEVER persisted — only actual differences create
    a row (Database Architecture doc: "only differences stored, volume ∝ actual
    changes"). ``ADDED`` rows have ``old_chunk_id = NULL``; ``REMOVED`` rows
    have ``new_chunk_id = NULL``.

    ``old_text``/``new_text`` are denormalized snapshots so the UI can render
    a diff without re-reading the chunk content — these survive chunk deletion
    (old/new_chunk_id use ON DELETE SET NULL, not CASCADE).
    """

    __tablename__ = "comparison_changes"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('ADDED','REMOVED','MODIFIED')",
            name="ck_comparison_changes_change_type",
        ),
        CheckConstraint(
            "severity IN ('MAJOR','MODERATE','MINOR')",
            name="ck_comparison_changes_severity",
        ),
        CheckConstraint(
            "change_type <> 'ADDED' OR old_chunk_id IS NULL",
            name="ck_comparison_changes_added_no_old",
        ),
        CheckConstraint(
            "change_type <> 'REMOVED' OR new_chunk_id IS NULL",
            name="ck_comparison_changes_removed_no_new",
        ),
        Index("ix_comparison_changes_comparison_severity", "comparison_id", "severity"),
        Index("ix_comparison_changes_comparison_section", "comparison_id", "section"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    comparison_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_comparisons.id", ondelete="CASCADE"),
        nullable=False,
    )
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    old_chunk_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_chunk_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    old_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="True when semantic comparison was computed on truncated text (§9.6)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    comparison: Mapped[DocumentComparison] = relationship(
        "DocumentComparison",
        back_populates="changes",
    )
    old_chunk: Mapped[Optional[DocumentChunk]] = relationship(
        "DocumentChunk",
        foreign_keys=[old_chunk_id],
        lazy="noload",
    )
    new_chunk: Mapped[Optional[DocumentChunk]] = relationship(
        "DocumentChunk",
        foreign_keys=[new_chunk_id],
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<ComparisonChange id={self.id!r} type={self.change_type!r} "
            f"severity={self.severity!r} section={self.section!r}>"
        )
