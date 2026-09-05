"""
SQLAlchemy ORM model for the document-summary domain (Phase 14).

Table:
  - DocumentSummary — one row per document_version_id (UNIQUE): the cited,
    structured summary (executive summary, key points, dates, roles,
    requirements, risks, topics) persisted as one JSONB blob.

Persistence shape (PHASE-14-IMPLEMENTATION-PLAN.md §4.1): a summary is
always fetched and rendered as one cohesive document — never filtered,
sorted, or paginated at the item level — so the structured content lives in
a single JSONB column (like document_comparisons.summary) rather than a
child table that would add join cost for zero benefit.

Lifecycle: PENDING → PROCESSING → COMPLETED, with regeneration updating the
row in place (the existing ``summary`` JSONB is NOT cleared until the new
result actually completes — Backend §43 "stale summary remains visible with
a warning").

``stale`` is set only by the re-chunking/re-embedding invalidation hook
(plan §4.6) and cleared on the next successful regeneration.

See:
  Database-Architecture-Design-Documentation.md §8 (multi-tenant shape)
  PHASE-14-IMPLEMENTATION-PLAN.md §4.2
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
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document, DocumentVersion
    from app.models.organization import Organization
    from app.models.user import User


class DocumentSummary(Base):
    """One persisted, cited summary per document version.

    ``summary`` is the full structured payload with inline resolved-citation
    objects per item; NULL until the first COMPLETED generation.  ``sampling``
    is the disclosure object (``{"sampled": bool, "strategy": ...,
    "included_section_ids": [...], "excluded_section_count": int}``) that the
    frontend renders as the long-document partial-state banner — sampling is
    disclosed, never silent (roadmap exit criterion 7).
    """

    __tablename__ = "document_summaries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')",
            name="ck_document_summaries_status",
        ),
        UniqueConstraint(
            "document_version_id",
            name="uq_document_summaries_version",
        ),
        Index("ix_document_summaries_org_status", "organization_id", "status"),
        Index("ix_document_summaries_document_id", "document_id"),
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
        comment="Tenant owner — every read/list query filters on it directly",
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        comment="Denormalized for 'summary status for this document' lookups",
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        comment="One row per version — regeneration updates in place",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PENDING",
        server_default="PENDING",
    )
    summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured payload with inline resolved citations; not cleared on regenerate until re-completed",
    )
    sampling: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment='Disclosure object: {"sampled", "strategy", "included_section_ids", "excluded_section_count"}',
    )
    model: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="LLM model used for the last successful generation (auditability)",
    )
    prompt_version: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="SUMMARY_PROMPT_VERSION used for the last successful generation",
    )
    stale: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Set by the re-chunking/re-embedding invalidation hook; cleared on regeneration",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    requested_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="User whose action most recently (re)triggered generation",
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
    requested_by_user: Mapped[User] = relationship(
        "User",
        foreign_keys=[requested_by],
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
            f"<DocumentSummary id={self.id!r} status={self.status!r} "
            f"version={self.document_version_id!r} stale={self.stale!r}>"
        )
