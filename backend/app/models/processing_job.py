"""
SQLAlchemy ORM model for background processing jobs.

Table: processing_jobs — the AUTHORITATIVE job record (PostgreSQL).
Redis/Arq queue entries are lightweight pointers (the job UUID) that are
disposable: the worker reconciliation sweep re-enqueues any PENDING/stuck
job found here with no live Redis entry.

See:
  Backend-Architecture-Documentation.md §23 (Background Processing Architecture)
  Database-Architecture-Design-Documentation.md §10 (ERD)
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from app.models.comparison import DocumentComparison
    from app.models.document import DocumentVersion
    from app.models.organization import Organization


class ProcessingJob(Base, TimestampMixin):
    """One row per job (pipeline stage or maintenance task).

    Lifecycle (Backend §47):
        PENDING → PROCESSING → COMPLETED
                     │
                     ├──→ FAILED  (retries exhausted)
                     └──→ RETRYING → PROCESSING (transient failure)

    RETRYING is deliberately distinct from PENDING so monitoring can
    distinguish "never yet attempted" from "failed N times, backing off".
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('EXTRACTION','OCR','CHUNKING','EMBEDDING','INDEXING',"
            "'COMPARISON','SUMMARY','CONFLICT_SCAN','PURGE')",
            name="ck_processing_jobs_job_type",
        ),
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','RETRYING','COMPLETED','FAILED')",
            name="ck_processing_jobs_status",
        ),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)",
            name="ck_processing_jobs_progress_range",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1",
            name="ck_processing_jobs_attempts",
        ),
        # Phase 12: comparison jobs must have comparison_id; non-comparison jobs must not
        CheckConstraint(
            "(job_type = 'COMPARISON') = (comparison_id IS NOT NULL)",
            name="ck_processing_jobs_comparison_pairing",
        ),
        Index("ix_processing_jobs_document_version_id", "document_version_id", "status"),
        Index("ix_processing_jobs_status", "status", "created_at"),
        Index("ix_processing_jobs_org_status", "organization_id", "status"),
        Index("ix_processing_jobs_comparison_id", "comparison_id"),
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
        comment="Tenant owner — carried explicitly in payloads, re-validated by workers",
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Phase 12 — set only for job_type=COMPARISON; document_version_id holds
    # the anchor (A) version for indexing/bookkeeping (§10, Gap 3).
    comparison_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_comparisons.id", ondelete="CASCADE"),
        nullable=True,
        comment="Set only for job_type=COMPARISON; document_version_id holds the anchor (A) version",
    )
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    progress: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    progress_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="First claim time (retries keep the original)",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Terminal transition time (COMPLETED or FAILED)",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="noload",
    )
    document_version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        foreign_keys=[document_version_id],
        lazy="noload",
    )
    comparison: Mapped[Optional[DocumentComparison]] = relationship(
        "DocumentComparison",
        foreign_keys=[comparison_id],
        lazy="noload",
    )

    @property
    def is_active(self) -> bool:
        """True while the job still has work or retries outstanding."""
        return self.status in ("PENDING", "PROCESSING", "RETRYING")

    @property
    def is_terminal(self) -> bool:
        return self.status in ("COMPLETED", "FAILED")

    def __repr__(self) -> str:
        return (
            f"<ProcessingJob id={self.id!r} type={self.job_type!r} "
            f"status={self.status!r} attempts={self.attempts}/{self.max_attempts}>"
        )
