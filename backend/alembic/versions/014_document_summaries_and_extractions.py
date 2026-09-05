"""document_summaries, document_extractions, document_extraction_items tables
+ processing_jobs extension.

Phase 14 — Advanced Document Intelligence.

New tables:
  - document_summaries        — one row per document_version_id (UNIQUE),
                                regenerated in place; structured summary
                                stored as one JSONB blob
  - document_extractions      — one row per extraction RUN (append-only,
                                audit history — deliberately NOT unique per
                                version)
  - document_extraction_items — N citation-grade extracted items per run
                                (parent + real child rows, mirroring
                                comparison_changes / conflict_statements)

Modified table:
  - processing_jobs  — adds nullable summary_id / extraction_id FKs (mirrors
    comparison_id), widens the job_type CHECK with 'STRUCTURED_EXTRACTION'
    (a NEW value, distinct from the Phase 5 ingestion text-extraction
    stage's 'EXTRACTION'), and adds pairing CHECK constraints.

Seeds:
  - summary:regenerate + extraction:create permissions, granted to the
    Admin and Editor system roles (Viewer deliberately excluded — mirrors
    comparison:create / conflict:resolve).

Revision ID: 014
Revises: 013
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic
revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

_JOB_TYPES_PRE_14 = (
    "EXTRACTION", "OCR", "CHUNKING", "EMBEDDING", "INDEXING",
    "COMPARISON", "SUMMARY", "CONFLICT_SCAN", "PURGE",
)
_JOB_TYPES_14 = _JOB_TYPES_PRE_14 + ("STRUCTURED_EXTRACTION",)


def upgrade() -> None:
    # ── document_summaries ────────────────────────────────────────────────────
    op.create_table(
        "document_summaries",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("summary", JSONB, nullable=True),
        sa.Column("sampling", JSONB, nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column(
            "stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "prompt_tokens", sa.Integer(), nullable=True,
            comment="Prompt tokens of the last successful generation (Phase 19 cost attribution)",
        ),
        sa.Column(
            "completion_tokens", sa.Integer(), nullable=True,
            comment="Completion tokens of the last successful generation (Phase 19 cost attribution)",
        ),
        sa.Column(
            "requested_by",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')",
            name="ck_document_summaries_status",
        ),
        sa.UniqueConstraint(
            "document_version_id",
            name="uq_document_summaries_version",
        ),
    )

    op.create_index(
        "ix_document_summaries_org_status",
        "document_summaries",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_document_summaries_document_id",
        "document_summaries",
        ["document_id"],
    )

    # ── document_extractions (extraction run header) ──────────────────────────
    op.create_table(
        "document_extractions",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "schema_key",
            sa.Text(),
            nullable=False,
            server_default="standard_v1",
            comment="Closed enum-of-one in V1; forward-compatible column",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column(
            "prompt_tokens", sa.Integer(), nullable=True,
            comment="Prompt tokens of the last successful run (Phase 19 cost attribution)",
        ),
        sa.Column(
            "completion_tokens", sa.Integer(), nullable=True,
            comment="Completion tokens of the last successful run (Phase 19 cost attribution)",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "requested_by",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')",
            name="ck_document_extractions_status",
        ),
        sa.CheckConstraint(
            "schema_key IN ('standard_v1')",
            name="ck_document_extractions_schema_key",
        ),
    )

    op.create_index(
        "ix_document_extractions_org_status",
        "document_extractions",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_document_extractions_version_created",
        "document_extractions",
        ["document_version_id", sa.text("created_at DESC")],
    )

    # ── document_extraction_items ─────────────────────────────────────────────
    op.create_table(
        "document_extraction_items",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "extraction_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_chunks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "page_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_pages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "page_number", sa.Integer(), nullable=False,
        ),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("quoted_text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("relevance_score", sa.Numeric(6, 5), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "category IN ('requirement','risk','date','party')",
            name="ck_document_extraction_items_category",
        ),
        sa.CheckConstraint(
            "page_number >= 1",
            name="ck_document_extraction_items_page_number",
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_start >= 0",
            name="ck_document_extraction_items_char_start",
        ),
        sa.CheckConstraint(
            "char_end IS NULL OR char_end >= 0",
            name="ck_document_extraction_items_char_end",
        ),
        sa.UniqueConstraint(
            "extraction_id",
            "category",
            "item_index",
            name="uq_document_extraction_items_run_category_index",
        ),
    )

    op.create_index(
        "ix_document_extraction_items_extraction_category",
        "document_extraction_items",
        ["extraction_id", "category"],
    )
    op.create_index(
        "ix_document_extraction_items_chunk_id",
        "document_extraction_items",
        ["chunk_id"],
    )

    # ── processing_jobs extension ─────────────────────────────────────────────
    op.add_column(
        "processing_jobs",
        sa.Column(
            "summary_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_summaries.id", ondelete="CASCADE"),
            nullable=True,
            comment="Set only for job_type=SUMMARY; document_version_id holds the summarized version",
        ),
    )
    op.add_column(
        "processing_jobs",
        sa.Column(
            "extraction_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_extractions.id", ondelete="CASCADE"),
            nullable=True,
            comment="Set only for job_type=STRUCTURED_EXTRACTION; document_version_id holds the extracted version",
        ),
    )
    # Widen the job-type CHECK: drop and recreate with 'STRUCTURED_EXTRACTION'
    # appended.  No existing row carries any new value, so validation against
    # existing data is instant (metadata-only operation).
    op.drop_constraint("ck_processing_jobs_job_type", "processing_jobs", type_="check")
    op.create_check_constraint(
        "ck_processing_jobs_job_type",
        "processing_jobs",
        f"job_type IN ({', '.join(repr(t) for t in _JOB_TYPES_14)})",
    )
    op.create_check_constraint(
        "ck_processing_jobs_summary_pairing",
        "processing_jobs",
        "(job_type = 'SUMMARY') = (summary_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_processing_jobs_extraction_pairing",
        "processing_jobs",
        "(job_type = 'STRUCTURED_EXTRACTION') = (extraction_id IS NOT NULL)",
    )
    op.create_index(
        "ix_processing_jobs_summary_id",
        "processing_jobs",
        ["summary_id"],
    )
    op.create_index(
        "ix_processing_jobs_extraction_id",
        "processing_jobs",
        ["extraction_id"],
    )

    _seed_phase14_permissions()


def _seed_phase14_permissions() -> None:
    """Seed summary:regenerate + extraction:create; grant to Admin + Editor.

    Mirrors migration 013's _seed_conflict_permission exactly (raw SQL
    INSERT ... ON CONFLICT DO NOTHING).  Viewer deliberately does not
    receive either permission — identical to comparison:create.
    """
    conn = op.get_bind()
    for key, description in (
        ("summary:regenerate", "Regenerate a document version's AI summary"),
        ("extraction:create", "Run structured-information extraction on a document version"),
    ):
        conn.execute(
            sa.text(
                "INSERT INTO permissions (id, key, description) "
                "VALUES (:id, :key, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"id": str(uuid.uuid4()), "key": key, "description": description},
        )
        # Re-fetch the permission id (in case ON CONFLICT hit an existing row)
        row = conn.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"),
            {"key": key},
        ).fetchone()
        if row is None:
            continue
        permission_id = row[0]
        for role_name in ("Admin", "Editor"):
            role_row = conn.execute(
                sa.text(
                    "SELECT id FROM roles WHERE name = :name "
                    "AND organization_id IS NULL"
                ),
                {"name": role_name},
            ).fetchone()
            if role_row is None:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"role_id": role_row[0], "permission_id": permission_id},
            )


def downgrade() -> None:
    # Reverse in dependency order.  The seeded permission rows and their
    # role_permissions grants are intentionally left in place on downgrade
    # (same retention policy as migrations 002/013 — permission data is not
    # tenant data and removing it could orphan audits).
    op.drop_index("ix_processing_jobs_extraction_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_summary_id", table_name="processing_jobs")
    op.drop_constraint(
        "ck_processing_jobs_extraction_pairing", "processing_jobs", type_="check"
    )
    op.drop_constraint(
        "ck_processing_jobs_summary_pairing", "processing_jobs", type_="check"
    )
    # Widen-back the job-type CHECK to the pre-Phase-14 list.  STRUCTURED_
    # EXTRACTION jobs have no version-anchor-independent existence — remove
    # them (and their extraction rows via CASCADE) before the constraint
    # narrows.
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM processing_jobs WHERE job_type = 'STRUCTURED_EXTRACTION'")
    )
    op.drop_constraint("ck_processing_jobs_job_type", "processing_jobs", type_="check")
    op.create_check_constraint(
        "ck_processing_jobs_job_type",
        "processing_jobs",
        f"job_type IN ({', '.join(repr(t) for t in _JOB_TYPES_PRE_14)})",
    )
    op.drop_column("processing_jobs", "extraction_id")
    op.drop_column("processing_jobs", "summary_id")

    op.drop_index(
        "ix_document_extraction_items_chunk_id", table_name="document_extraction_items"
    )
    op.drop_index(
        "ix_document_extraction_items_extraction_category",
        table_name="document_extraction_items",
    )
    op.drop_table("document_extraction_items")

    op.drop_index(
        "ix_document_extractions_version_created", table_name="document_extractions"
    )
    op.drop_index(
        "ix_document_extractions_org_status", table_name="document_extractions"
    )
    op.drop_table("document_extractions")

    op.drop_index(
        "ix_document_summaries_document_id", table_name="document_summaries"
    )
    op.drop_index(
        "ix_document_summaries_org_status", table_name="document_summaries"
    )
    op.drop_table("document_summaries")
