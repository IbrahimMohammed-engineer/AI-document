"""
Migration 004 — Documents, Document Versions, Document Tags, Collections.

Creates the core document management schema:
  - documents        — logical business document (stable across versions)
  - document_versions — one row per uploaded file (processing pipeline tracks here)
  - document_tags    — N:M join (document_id, tag)
  - collections      — named document groups (org-scoped)
  - collection_documents — N:M join (collection_id, document_id)

See:
  Database-Architecture-Design-Documentation.md §13–14 (documents/versions)
  Database-Architecture-Design-Documentation.md §19 (collections)
  Database-Architecture-Design-Documentation.md §27 (indexes)
  Database-Architecture-Design-Documentation.md §28 (constraints)

Revision ID: 004
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | None = None
depends_on: str | None = None

# Allowed document_type values (CHECK constraint)
DOCUMENT_TYPES = (
    "policy", "procedure", "sop", "contract", "technical",
    "regulatory", "hr", "marketing", "other",
)

# Allowed access_level values (CHECK constraint)
ACCESS_LEVELS = ("organization", "restricted", "private")

# Allowed document_versions.status values (CHECK constraint)
# Includes CHUNKING (Backend §47) which is missing from the DB doc's inline list
VERSION_STATUSES = (
    "UPLOADED", "PROCESSING", "EXTRACTING", "OCR",
    "CHUNKING", "EMBEDDING", "INDEXING", "READY", "FAILED",
)

# Allowed documents.status values (CHECK constraint)
DOCUMENT_STATUSES = ("active", "archived")


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────────────
    # 1. documents — logical business document
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Tenant owner — all queries must be scoped by this column",
        ),
        sa.Column("name", sa.Text(), nullable=False, comment="Display name, e.g. 'Marketing Policy'"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "document_type",
            sa.String(50),
            nullable=False,
            comment=(
                "Constrained set — "
                + ", ".join(f"'{t}'" for t in DOCUMENT_TYPES)
            ),
        ),
        sa.Column("department", sa.Text(), nullable=True, comment="Free-text department label"),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
            comment="Document-level lifecycle: active | archived",
        ),
        sa.Column(
            "access_level",
            sa.String(20),
            nullable=False,
            server_default="organization",
            comment="organization | restricted | private",
        ),
        # Denormalized pointer to current/latest effective version.
        # Nullable: a freshly-created document has no version yet.
        # Application logic updates this transactionally when a new version reaches READY.
        sa.Column(
            "current_version_id",
            UUID(as_uuid=False),
            nullable=True,
            comment=(
                "Denormalized pointer to latest effective version. "
                "FK added after document_versions exists (see below). "
                "NULL = no version yet published as current."
            ),
        ),
        sa.Column(
            "owner_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="User who owns/created this logical document",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-delete marker — see DB §29",
        ),
        # CHECK constraints
        sa.CheckConstraint(
            f"document_type IN ({', '.join(repr(t) for t in DOCUMENT_TYPES)})",
            name="ck_documents_document_type",
        ),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in DOCUMENT_STATUSES)})",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            f"access_level IN ({', '.join(repr(a) for a in ACCESS_LEVELS)})",
            name="ck_documents_access_level",
        ),
    )

    # Indexes for documents — DB §27
    op.create_index("ix_documents_organization_id", "documents", ["organization_id"])
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
    op.create_index(
        "ix_documents_org_status",
        "documents",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_documents_org_type",
        "documents",
        ["organization_id", "document_type"],
    )
    op.create_index(
        "ix_documents_org_created",
        "documents",
        ["organization_id", "created_at"],
    )
    # Partial index for the dominant "active documents" query shape
    op.create_index(
        "ix_documents_org_active",
        "documents",
        ["organization_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 2. document_versions — one row per uploaded file / processing run
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "document_versions",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_number",
            sa.Integer(),
            nullable=False,
            comment="Monotonically increasing per document_id (1, 2, 3…) — app-assigned",
        ),
        sa.Column(
            "version_label",
            sa.Text(),
            nullable=True,
            comment="Human-facing label, e.g. '2026' or 'v2.1'",
        ),
        sa.Column(
            "effective_date",
            sa.Date(),
            nullable=True,
            comment="When this version's content became effective",
        ),
        sa.Column(
            "expiration_date",
            sa.Date(),
            nullable=True,
            comment="When this version stopped being effective (NULL = still effective)",
        ),
        sa.Column(
            "storage_key",
            sa.Text(),
            nullable=False,
            comment="Object storage path — immutable once written (corrections are new versions)",
        ),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "checksum_sha256",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex digest — used for duplicate detection",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="UPLOADED",
            comment=(
                "Pipeline status: "
                + " | ".join(VERSION_STATUSES)
            ),
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Populated when status = FAILED",
        ),
        sa.Column(
            "page_count",
            sa.Integer(),
            nullable=True,
            comment="Populated after extraction (Phase 5)",
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # CHECK constraint on status
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in VERSION_STATUSES)})",
            name="ck_document_versions_status",
        ),
        # UNIQUE: exactly one row per version_number per document
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_versions_doc_ver"),
    )

    # Indexes for document_versions — DB §27
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index(
        "ix_document_versions_status",
        "document_versions",
        ["document_id", "status"],
    )

    # ── Add FK: documents.current_version_id → document_versions.id ──────────
    # Added after document_versions table exists (circular dependency split).
    op.create_foreign_key(
        "fk_documents_current_version_id",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 3. document_tags — N:M join (document_id, tag)
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "document_tags",
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "tag", name="pk_document_tags"),
    )

    op.create_index("ix_document_tags_document_id", "document_tags", ["document_id"])
    # Index on tag for tag-based filtering across documents in an org
    op.create_index("ix_document_tags_tag", "document_tags", ["tag"])

    # ─────────────────────────────────────────────────────────────────────────
    # 4. collections — named document groups (org-scoped)
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "collections",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False, comment="e.g. 'HR Policies', 'Vendor Contracts'"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # A collection name is unique within an organization
        sa.UniqueConstraint("organization_id", "name", name="uq_collections_org_name"),
    )

    op.create_index("ix_collections_organization_id", "collections", ["organization_id"])

    # ─────────────────────────────────────────────────────────────────────────
    # 5. collection_documents — N:M join (collection_id, document_id)
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "collection_documents",
        sa.Column(
            "collection_id",
            UUID(as_uuid=False),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "collection_id", "document_id", name="pk_collection_documents"
        ),
    )

    op.create_index(
        "ix_collection_documents_document_id",
        "collection_documents",
        ["document_id"],
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("collection_documents")
    op.drop_table("collections")
    op.drop_table("document_tags")

    # Remove the circular FK before dropping document_versions / documents
    op.drop_constraint("fk_documents_current_version_id", "documents", type_="foreignkey")
    op.drop_table("document_versions")
    op.drop_table("documents")
