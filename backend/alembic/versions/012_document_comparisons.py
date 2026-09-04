"""document_comparisons and comparison_changes tables + processing_jobs extension.

Phase 12 — Document Versioning and Comparison (M6 vertical slice).

New tables:
  - document_comparisons  — one row per unique (org, versionA, versionB) pair
  - comparison_changes    — individual detected changes; FK-ON DELETE CASCADE

Modified table:
  - processing_jobs  — adds nullable comparison_id FK for COMPARISON job type

Revision ID: 012
Revises: 011
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic
revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── document_comparisons ──────────────────────────────────────────────────
    op.create_table(
        "document_comparisons",
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
            "document_a_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_b_version_id",
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
            name="ck_document_comparisons_status",
        ),
        sa.CheckConstraint(
            "document_a_version_id <> document_b_version_id",
            name="ck_document_comparisons_distinct_versions",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "document_a_version_id",
            "document_b_version_id",
            name="uq_document_comparisons_pair",
        ),
    )

    op.create_index(
        "ix_document_comparisons_org_status",
        "document_comparisons",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_document_comparisons_a",
        "document_comparisons",
        ["document_a_version_id"],
    )
    op.create_index(
        "ix_document_comparisons_b",
        "document_comparisons",
        ["document_b_version_id"],
    )

    # ── comparison_changes ────────────────────────────────────────────────────
    op.create_table(
        "comparison_changes",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "comparison_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_comparisons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column(
            "old_chunk_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "new_chunk_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("old_text", sa.Text(), nullable=True),
        sa.Column("new_text", sa.Text(), nullable=True),
        sa.Column(
            "truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "change_type IN ('ADDED','REMOVED','MODIFIED')",
            name="ck_comparison_changes_change_type",
        ),
        sa.CheckConstraint(
            "severity IN ('MAJOR','MODERATE','MINOR')",
            name="ck_comparison_changes_severity",
        ),
        sa.CheckConstraint(
            "change_type <> 'ADDED' OR old_chunk_id IS NULL",
            name="ck_comparison_changes_added_no_old",
        ),
        sa.CheckConstraint(
            "change_type <> 'REMOVED' OR new_chunk_id IS NULL",
            name="ck_comparison_changes_removed_no_new",
        ),
    )

    op.create_index(
        "ix_comparison_changes_comparison_severity",
        "comparison_changes",
        ["comparison_id", "severity"],
    )
    op.create_index(
        "ix_comparison_changes_comparison_section",
        "comparison_changes",
        ["comparison_id", "section"],
    )

    # ── processing_jobs extension (Gap 3 — §4.5) ─────────────────────────────
    op.add_column(
        "processing_jobs",
        sa.Column(
            "comparison_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_comparisons.id", ondelete="CASCADE"),
            nullable=True,
            comment="Set only for job_type=COMPARISON; document_version_id holds the anchor (A) version",
        ),
    )
    op.create_check_constraint(
        "ck_processing_jobs_comparison_pairing",
        "processing_jobs",
        "(job_type = 'COMPARISON') = (comparison_id IS NOT NULL)",
    )
    op.create_index(
        "ix_processing_jobs_comparison_id",
        "processing_jobs",
        ["comparison_id"],
    )


def downgrade() -> None:
    # Reverse in dependency order
    op.drop_index("ix_processing_jobs_comparison_id", table_name="processing_jobs")
    op.drop_constraint(
        "ck_processing_jobs_comparison_pairing", "processing_jobs", type_="check"
    )
    op.drop_column("processing_jobs", "comparison_id")

    op.drop_index("ix_comparison_changes_comparison_section", table_name="comparison_changes")
    op.drop_index("ix_comparison_changes_comparison_severity", table_name="comparison_changes")
    op.drop_table("comparison_changes")

    op.drop_index("ix_document_comparisons_b", table_name="document_comparisons")
    op.drop_index("ix_document_comparisons_a", table_name="document_comparisons")
    op.drop_index("ix_document_comparisons_org_status", table_name="document_comparisons")
    op.drop_table("document_comparisons")
