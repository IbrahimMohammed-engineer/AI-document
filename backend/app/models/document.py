"""
SQLAlchemy ORM models for the document management domain.

Tables:
  - Document          — logical business document (stable across versions)
  - DocumentVersion   — one row per uploaded file / processing run
  - DocumentPage      — extracted/OCR'd page content + geometry (Phase 5)
  - DocumentSection   — hierarchical TOC node per version (Phase 6)
  - DocumentChunk     — the retrieval unit with full provenance (Phase 6)
  - DocumentTag       — N:M join (document_id, tag)
  - Collection        — named document groups (org-scoped)
  - CollectionDocument — N:M join (collection_id, document_id)

See:
  Database-Architecture-Design-Documentation.md §13–14 (documents/versions)
  Database-Architecture-Design-Documentation.md §15 (pages, sections)
  Database-Architecture-Design-Documentation.md §16 (chunks)
  Database-Architecture-Design-Documentation.md §19 (collections)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false as sql_false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

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


# ── DocumentPage ──────────────────────────────────────────────────────────────

class DocumentPage(Base):
    """One extracted (or OCR'd) page of a document version.

    Written incrementally by the EXTRACTION stage (Phase 5), streamed in
    batches so worker memory stays bounded and crashes resume from the last
    persisted page. Pages are immutable once written for a version; explicit
    retry semantics replace them deliberately.

    `page_metadata` maps to the DB column `metadata` (JSONB): OCR provider,
    confidence (internal-only quality signal), per-line bounding boxes for
    future citation highlighting, and the explicit "OCR failed" marker.

    See Database-Architecture-Design-Documentation.md §15.
    """

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "page_number",
            name="uq_document_pages_version_page",
        ),
        CheckConstraint(
            "page_number >= 1",
            name="ck_document_pages_page_number",
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-indexed reading order",
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
        comment="Extracted or OCR'd raw text for the page",
    )
    ocr_used: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sql_false(),
        comment="True if this page's text came from OCR rather than native extraction",
    )
    render_storage_key: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional pre-rendered page image (perf optimization — later phase)",
    )
    width: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
        comment="Page width (points) — normalizes highlight bounding boxes",
    )
    height: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
        comment="Page height (points) — normalizes highlight bounding boxes",
    )
    # NOTE: attribute is `page_metadata` (Base.metadata is reserved in
    # SQLAlchemy); the DB column is named `metadata`.
    page_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="OCR provider/confidence/line boxes (internal) + ocr_failed marker",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentPage version={self.document_version_id!r} "
            f"page={self.page_number} ocr={self.ocr_used}>"
        )


# ── Minimal pgvector DDL mirror ──────────────────────────────────────────────

class PgVector(UserDefinedType):
    """DDL-only mirror of the pgvector column type on document_chunks.embedding.

    Dimensionality is pinned by migration 007 (DB §17 — the model must be
    re-created and every chunk re-embedded to change it). This type exists so
    the ORM can resolve the column; Phase 7's embedding client owns real
    vector values.
    """

    def get_col_spec(self) -> str:
        return "vector(1536)"

    def __repr__(self) -> str:
        return "PgVector(1536)"


# ── DocumentSection ──────────────────────────────────────────────────────────

class DocumentSection(Base):
    """One node of a version's hierarchical table-of-contents tree.

    Produced by the heuristic structure detector (Phase 6): a self-referencing
    adjacency list — unbounded TOC depth, trivial "immediate children" reads
    for the TOC tree, recursive CTEs for the rare full ancestor path
    (citation breadcrumbs). Documents with no detectable structure simply
    have ZERO rows here — an explicitly supported, non-error state
    (Backend §20; FE §6.5 "No structure detected").

    sort_order is the reading-order index across the whole tree, which keeps
    sibling order correct without relying on section numbers being sortable.

    See Database-Architecture-Design-Documentation.md §15.
    """

    __tablename__ = "document_sections"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "sort_order",
            name="uq_document_sections_version_order",
        ),
        CheckConstraint(
            "start_page >= 1",
            name="ck_document_sections_start_page",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_document_sections_sort_order",
        ),
        Index(
            "ix_document_sections_version_parent",
            "document_version_id",
            "parent_section_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_section_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_sections.id", ondelete="CASCADE"),
        nullable=True,
        comment="NULL = top-level section",
    )
    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Heading text (numbering prefix stripped)",
    )
    section_number: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="e.g. '4.2' — text: numbering schemes vary (4.2, IV.b, Appendix A)",
    )
    start_page: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-indexed page the heading appears on",
    )
    end_page: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Page where the next same-or-higher-level heading begins",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Reading-order index across the whole tree",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        lazy="noload",
    )
    parent: Mapped[Optional[DocumentSection]] = relationship(
        "DocumentSection",
        remote_side="DocumentSection.id",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentSection version={self.document_version_id!r} "
            f"no={self.section_number!r} title={self.title!r}>"
        )


# ── DocumentChunk ────────────────────────────────────────────────────────────

class DocumentChunk(Base):
    """The retrieval unit — full provenance back to page(s) and section.

    Written by the CHUNKING stage (Phase 6) with upsert idempotency on
    (document_version_id, chunk_index): re-runs overwrite, never duplicate
    (Backend §49). `organization_id` is denormalized from the owning
    document so every retrieval query filters tenant directly — drift from
    the parent document's org is made structurally impossible by the
    trg_chunk_org_consistency trigger (DB §28), not merely forbidden.

    `embedding`/`embedding_model` stay NULL until the Phase 7 embedding
    stage fills them; `content_tsv` is a DB-generated tsvector (read-only —
    never written by application code) that Phase 8's keyword search indexes.

    See Database-Architecture-Design-Documentation.md §16.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "chunk_index",
            name="uq_document_chunks_version_index",
        ),
        CheckConstraint(
            "chunk_index >= 0",
            name="ck_document_chunks_chunk_index",
        ),
        CheckConstraint(
            "token_count >= 0",
            name="ck_document_chunks_token_count",
        ),
        Index("ix_document_chunks_document_version_id", "document_version_id"),
        Index("ix_document_chunks_page_id", "page_id"),
        Index("ix_document_chunks_section_id", "section_id"),
        Index("ix_document_chunks_organization_id", "organization_id"),
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
        comment="DENORMALIZED from the owning document — trigger-enforced consistent",
    )
    document_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_pages.id", ondelete="CASCADE"),
        nullable=False,
        comment="The chunk's starting page",
    )
    end_page_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_pages.id", ondelete="CASCADE"),
        nullable=True,
        comment="Set only when the chunk spans multiple pages",
    )
    section_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("document_sections.id", ondelete="SET NULL"),
        nullable=True,
        comment="Nullable — preambles/unstructured documents have no section",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Sequential reading order within the version, 0-based",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hex of content — dedup + Phase 12 cheap change pre-diff",
    )
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Tokenizer token count — LLM context-budget accounting",
    )
    # NOTE: attribute is `chunk_metadata` (Base.metadata is reserved in
    # SQLAlchemy); the DB column is named `metadata`.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="Heading path, table/list flags, bounding boxes, forced-split notes",
    )
    embedding: Mapped[Optional[Any]] = mapped_column(
        PgVector,
        nullable=True,
        comment="pgvector(1536) — NULL until the Phase 7 embedding stage",
    )
    embedding_model: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Set together with embedding by Phase 7 (model/version provenance)",
    )
    # DB-generated, read-only: populated by the STORED generated-column
    # expression; application code never writes it (Phase 8 adds the GIN idx).
    content_tsv: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    version: Mapped[DocumentVersion] = relationship(
        "DocumentVersion",
        lazy="noload",
    )
    page: Mapped[DocumentPage] = relationship(
        "DocumentPage",
        foreign_keys=[page_id],
        lazy="noload",
    )
    end_page: Mapped[Optional[DocumentPage]] = relationship(
        "DocumentPage",
        foreign_keys=[end_page_id],
        lazy="noload",
    )
    section: Mapped[Optional[DocumentSection]] = relationship(
        "DocumentSection",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentChunk version={self.document_version_id!r} "
            f"index={self.chunk_index} tokens={self.token_count}>"
        )


# ── DocumentPermission (Phase 16) ─────────────────────────────────────────────

class DocumentPermission(Base):
    """Explicit per-user access grant on a document (Phase 16).

    The RESTRICTED access-level matrix: the owner always has access; every
    OTHER org member is denied unless a row exists here for
    (document_id, user_id) with permission_type in (read, write, admin) and
    either no expiry or a live (future) ``expires_at``.

    Grants are consulted by ``AuthorizationService.resolve_allowed_documents``
    (retrieval gate) and ``authorize_document_version`` (point lookups).
    """

    __tablename__ = "document_permissions"
    __table_args__ = (
        CheckConstraint(
            "permission_type IN ('read','write','admin')",
            name="ck_document_permissions_type",
        ),
        UniqueConstraint(
            "document_id", "user_id", name="uq_document_permissions_doc_user"
        ),
        Index(
            "ix_document_permissions_lookup",
            "document_id",
            "user_id",
        ),
        Index(
            "ix_document_permissions_organization_id",
            "organization_id",
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
        comment="DENORMALIZED tenant scope — grants never cross organizations",
    )
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Grantee — must belong to the same organization",
    )
    permission_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="read",
        server_default="read",
        comment="read | write | admin",
    )
    granted_by: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="The owner/admin who created the grant",
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="NULL = grant never expires; past value = grant dormant",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    document: Mapped[Document] = relationship(
        "Document", foreign_keys=[document_id], lazy="noload"
    )
    grantee: Mapped[User] = relationship(
        "User", foreign_keys=[user_id], lazy="noload"
    )
    granter: Mapped[User] = relationship(
        "User", foreign_keys=[granted_by], lazy="noload"
    )

    @property
    def is_live(self) -> bool:
        """True when the grant currently confers access (expiry-aware)."""
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.now(tz=timezone.utc)

    def __repr__(self) -> str:
        return (
            f"<DocumentPermission doc={self.document_id!r} "
            f"user={self.user_id!r} type={self.permission_type!r}>"
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
