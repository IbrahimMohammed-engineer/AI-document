"""
SQLAlchemy ORM models for the document management domain.

Tables:
  - Document          — logical business document (stable across versions)
  - DocumentVersion   — one row per uploaded file / processing run
  - DocumentTag       — N:M join (document_id, tag)
  - Collection        — named document groups (org-scoped)
  - CollectionDocument — N:M join (collection_id, document_id)

See:
  Database-Architecture-Design-Documentation.md §13–14 (documents/versions)
  Database-Architecture-Design-Documentation.md §19 (collections)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


# ── Document ──────────────────────────────────────────────────────────────────

class Document(Base, TimestampMixin):
    """Logical/business document — stable identity across all its versions.

    The actual file content lives in DocumentVersion rows.
    Soft-deleted via deleted_at — excluded from all active queries.

    See Database-Architecture-Design-Documentation.md §13.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "document_type IN ('policy','procedure','sop','contract','technical',"
            "'regulatory','hr','marketing','other')",
            name="ck_documents_document_type",
        ),
        CheckConstraint(
            "status IN ('active','archived')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "access_level IN ('organization','restricted','private')",
            name="ck_documents_access_level",
        ),
        Index("ix_documents_organization_id", "organization_id"),
        Index("ix_documents_owner_id", "owner_id"),
        Index("ix_documents_org_status", "organization_id", "status"),
        Index("ix_documents_org_type", "organization_id", "document_type"),
        Index("ix_documents_org_created", "organization_id", "created_at"),
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
        comment="Tenant owner",
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="policy|procedure|sop|contract|technical|regulatory|hr|marketing|other",
    )
    department: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    access_level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="organization",
        server_default="organization",
    )
    # Denormalized pointer to the current effective version.
    # FK is added AFTER document_versions table exists (circular dependency).
    current_version_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Latest published effective version — zero-join current-version lookup",
    )
    owner_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft delete — documents.deleted_at IS NULL in all active queries",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="noload",
    )
    owner: Mapped[User] = relationship(
        "User",
        foreign_keys=[owner_id],
        lazy="noload",
    )
    current_version: Mapped[Optional[DocumentVersion]] = relationship(
        "DocumentVersion",
        foreign_keys=[current_version_id],
        lazy="noload",
        primaryjoin="Document.current_version_id == DocumentVersion.id",
    )
    versions: Mapped[list[DocumentVersion]] = relationship(
        "DocumentVersion",
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
        lazy="noload",
        cascade="all, delete-orphan",
        order_by="DocumentVersion.version_number",
    )
    tags: Mapped[list[DocumentTag]] = relationship(
        "DocumentTag",
        back_populates="document",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:
        return f"<Document id={self.id!r} name={self.name!r}>"


# ── DocumentVersion ───────────────────────────────────────────────────────────

class DocumentVersion(Base):
    """One row per uploaded file / processing run.

    The processing pipeline updates `status` as it moves through stages.
    storage_key is immutable once written — corrections are new versions.

    See Database-Architecture-Design-Documentation.md §14.
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UPLOADED','PROCESSING','EXTRACTING','OCR',"
            "'CHUNKING','EMBEDDING','INDEXING','READY','FAILED')",
            name="ck_document_versions_status",
        ),
        UniqueConstraint(
            "document_id", "version_number",
            name="uq_document_versions_doc_ver",
        ),
        Index("ix_document_versions_document_id", "document_id"),
        Index("ix_document_versions_status", "document_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Monotonically increasing per document_id (1, 2, 3…)",
    )
    version_label: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-facing label, e.g. '2026' or 'v2.1'",
    )
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    storage_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Object storage path — immutable once written",
    )
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="SHA-256 hex digest — used for duplicate detection",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="UPLOADED",
        server_default="UPLOADED",
        comment="Pipeline status — updated by processing workers",
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Populated when status = FAILED",
    )
    page_count: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Populated after extraction (Phase 5)",
    )
    created_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    document: Mapped[Document] = relationship(
        "Document",
        back_populates="versions",
        foreign_keys=[document_id],
        lazy="noload",
    )
    uploader: Mapped[User] = relationship(
        "User",
        foreign_keys=[created_by],
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentVersion id={self.id!r} "
            f"doc={self.document_id!r} v{self.version_number} status={self.status!r}>"
        )


# ── DocumentTag ───────────────────────────────────────────────────────────────

class DocumentTag(Base):
    """N:M join: a document can have many tags; tags can appear on many documents.

    See Database-Architecture-Design-Documentation.md §13.
    """

    __tablename__ = "document_tags"
    __table_args__ = (
        Index("ix_document_tags_document_id", "document_id"),
        Index("ix_document_tags_tag", "tag"),
    )

    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)

    # Relationships
    document: Mapped[Document] = relationship(
        "Document",
        back_populates="tags",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<DocumentTag doc={self.document_id!r} tag={self.tag!r}>"


# ── Collection ────────────────────────────────────────────────────────────────

class Collection(Base, TimestampMixin):
    """Named document group scoped to an organization.

    A document may belong to multiple collections (N:M via CollectionDocument).
    Collections are an organizing/filtering construct — not exclusive folders.

    See Database-Architecture-Design-Documentation.md §19.
    """

    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_collections_org_name"),
        Index("ix_collections_organization_id", "organization_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    memberships: Mapped[list[CollectionDocument]] = relationship(
        "CollectionDocument",
        back_populates="collection",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Collection id={self.id!r} name={self.name!r}>"


# ── CollectionDocument ────────────────────────────────────────────────────────

class CollectionDocument(Base):
    """N:M join: a collection contains many documents; a document may belong to many collections.

    See Database-Architecture-Design-Documentation.md §19.
    """

    __tablename__ = "collection_documents"
    __table_args__ = (
        Index("ix_collection_documents_document_id", "document_id"),
    )

    collection_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("collections.id", ondelete="CASCADE"),
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
        server_default=func.now(),
    )

    # Relationships
    collection: Mapped[Collection] = relationship(
        "Collection",
        back_populates="memberships",
        lazy="noload",
    )
    document: Mapped[Document] = relationship(
        "Document",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<CollectionDocument col={self.collection_id!r} doc={self.document_id!r}>"
