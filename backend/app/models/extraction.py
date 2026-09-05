"""
SQLAlchemy ORM models for the structured-extraction domain (Phase 14).

Tables:
  - DocumentExtraction      — one row per extraction RUN (append-only audit
                              history; deliberately NOT unique per version —
                              unlike document_summaries' one-row-per-version
                              shape, a run is an immutable historical record)
  - DocumentExtractionItem  — N citation-grade extracted items per run, the
                              "diff/statement"-shaped data this schema family
                              models as real rows (mirrors comparison_changes
                              / conflict_statements) because the UI filters
                              by category and links to individual items

Item provenance mirrors the Citation / ConflictStatement FK policy exactly
(NOT NULL + ON DELETE RESTRICT everywhere): an extracted item's evidence
cannot be silently deleted out from under it.  ``quoted_text`` is a
denormalized snapshot of the REAL chunk content (never model-generated).

See:
  Database-Architecture-Design-Documentation.md §26 (provenance FK policy)
  PHASE-14-IMPLEMENTATION-PLAN.md §4.3, §4.4
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import (
        Document,
        DocumentChunk,
        DocumentPage,
        DocumentVersion,
    )
    from app.models.organization import Organization
    from app.models.user import User


class DocumentExtraction(Base):
    """One structured-information extraction run over a document version."""

    __tablename__ = "document_extractions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')",
            name="ck_document_extractions_status",
        ),
        CheckConstraint(
            "schema_key IN ('standard_v1')",
            name="ck_document_extractions_schema_key",
        ),
        Index(
            "ix_document_extractions_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_document_extractions_version_created",
            "document_version_id",
            "created_at",
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
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        comment="NOT unique — multiple runs per version are the point (audit history)",
    )
    schema_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="standard_v1",
        server_default="standard_v1",
        comment="Closed enum-of-one in V1; forward-compatible column",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="PENDING",
        server_default="PENDING",
    )
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    items: Mapped[list[DocumentExtractionItem]] = relationship(
        "DocumentExtractionItem",
        back_populates="extraction",
        cascade="all, delete-orphan",
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
            f"<DocumentExtraction id={self.id!r} status={self.status!r} "
            f"version={self.document_version_id!r} schema={self.schema_key!r}>"
        )


class DocumentExtractionItem(Base):
    """One extracted item (requirement / risk / date / party) within a run.

    Every item carries full citation-shaped provenance: the REAL chunk text
    it is grounded in (``quoted_text`` — from the same ``find_quoted_span``
    mechanism as chat citations, never model-generated), exact character
    offsets when a sub-span was picked, and page/section navigation fields.
    """

    __tablename__ = "document_extraction_items"
    __table_args__ = (
        CheckConstraint(
            "category IN ('requirement','risk','date','party')",
            name="ck_document_extraction_items_category",
        ),
        CheckConstraint(
            "page_number >= 1",
            name="ck_document_extraction_items_page_number",
        ),
        CheckConstraint(
            "char_start IS NULL OR char_start >= 0",
            name="ck_document_extraction_items_char_start",
        ),
        CheckConstraint(
            "char_end IS NULL OR char_end >= 0",
            name="ck_document_extraction_items_char_end",
        ),
        UniqueConstraint(
            "extraction_id",
            "category",
            "item_index",
            name="uq_document_extraction_items_run_category_index",
        ),
        Index(
            "ix_document_extraction_items_extraction_category",
            "extraction_id",
            "category",
        ),
        Index("ix_document_extraction_items_chunk_id", "chunk_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    extraction_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_extractions.id", ondelete="CASCADE"),
        nullable=False,
        comment="Items have no existence outside their run",
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    item_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="0-based ordering within (extraction_id, category)",
    )
    label: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The item's primary text/value (requirement sentence, party name, date value)",
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Category-specific extras (ISO date value, party role, risk severity) — deliberately schema-light",
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
        comment="RESTRICT — evidence cannot be silently deleted (mirrors citations.chunk_id)",
    )
    page_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_pages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quoted_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Real chunk text (never model-generated) — find_quoted_span mechanism",
    )
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
    extraction: Mapped[DocumentExtraction] = relationship(
        "DocumentExtraction",
        back_populates="items",
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
            f"<DocumentExtractionItem id={self.id!r} run={self.extraction_id!r} "
            f"category={self.category!r} index={self.item_index}>"
        )
