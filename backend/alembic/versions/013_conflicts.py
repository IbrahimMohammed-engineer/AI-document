"""conflicts and conflict_statements tables + processing_jobs extension.

Phase 13 — Conflict Detection (M6-adjacent vertical slice).

New tables:
  - conflicts            — one row per detected contradiction (aggregate root)
  - conflict_statements  — N citation-grade evidence statements per conflict

Modified table:
  - processing_jobs  — document_version_id becomes nullable (org-wide
    CONFLICT_SCAN jobs are anchored to an organization, not a version);
    adds a nullable checkpoint JSONB column (document-level scan-resume
    cursor + running counters); adds a CHECK constraint requiring
    document_version_id for every job type EXCEPT CONFLICT_SCAN.

Seeds:
  - conflict:resolve permission, granted to the Admin and Editor system
    roles (Viewer deliberately excluded — mirrors comparison:create).

Revision ID: 013
Revises: 012
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic
revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── conflicts ─────────────────────────────────────────────────────────────
    op.create_table(
        "conflicts",
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
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("detection_method", sa.Text(), nullable=False),
        sa.Column(
            "resolved_by",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "severity IN ('MAJOR','MODERATE','MINOR')",
            name="ck_conflicts_severity",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','REVIEWED','DISMISSED')",
            name="ck_conflicts_status",
        ),
        sa.CheckConstraint(
            "detection_method IN ('BACKGROUND_SCAN','COMPARISON_DERIVED','RETRIEVAL_TIME')",
            name="ck_conflicts_detection_method",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolved_by IS NULL AND resolved_at IS NULL) "
            "OR (status <> 'OPEN' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_conflicts_resolution_fields",
        ),
    )

    op.create_index(
        "ix_conflicts_org_status",
        "conflicts",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_conflicts_org_detected",
        "conflicts",
        ["organization_id", sa.text("detected_at DESC")],
    )

    # ── conflict_statements ───────────────────────────────────────────────────
    op.create_table(
        "conflict_statements",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "conflict_id",
            UUID(as_uuid=False),
            sa.ForeignKey("conflicts.id", ondelete="CASCADE"),
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
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("statement_text", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "conflict_id",
            "chunk_id",
            name="uq_conflict_statements_conflict_chunk",
        ),
    )

    op.create_index(
        "ix_conflict_statements_conflict_id",
        "conflict_statements",
        ["conflict_id"],
    )
    op.create_index(
        "ix_conflict_statements_chunk_id",
        "conflict_statements",
        ["chunk_id"],
    )
    op.create_index(
        "ix_conflict_statements_document_version_id",
        "conflict_statements",
        ["document_version_id"],
    )

    # ── processing_jobs extension (§9.3 — org-wide job shape, Gap 2) ─────────
    op.alter_column(
        "processing_jobs",
        "document_version_id",
        existing_type=UUID(as_uuid=False),
        nullable=True,
    )
    op.add_column(
        "processing_jobs",
        sa.Column(
            "checkpoint",
            JSONB,
            nullable=True,
            comment="CONFLICT_SCAN resume cursor + running counters (document-level granularity)",
        ),
    )
    op.create_check_constraint(
        "ck_processing_jobs_version_required",
        "processing_jobs",
        "(job_type = 'CONFLICT_SCAN' OR document_version_id IS NOT NULL)",
    )

    _seed_conflict_permission()


def _seed_conflict_permission() -> None:
    """Seed the conflict:resolve permission and grant it to Admin + Editor.

    Mirrors migration 002's _seed_permissions/_seed_system_roles pattern
    exactly (raw SQL INSERT ... ON CONFLICT DO NOTHING).  Viewer deliberately
    does not receive the permission — identical to comparison:create.
    """
    conn = op.get_bind()
    perm_id = str(uuid.uuid4())
    conn.execute(
        sa.text(
            "INSERT INTO permissions (id, key, description) "
            "VALUES (:id, :key, :description) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {
            "id": perm_id,
            "key": "conflict:resolve",
            "description": "Review, dismiss, and resolve detected conflicts",
        },
    )
    # Re-fetch the permission id (in case ON CONFLICT hit an existing row)
    row = conn.execute(
        sa.text("SELECT id FROM permissions WHERE key = 'conflict:resolve'")
    ).fetchone()
    actual_perm_id = row[0]
    for role_name in ("Admin", "Editor"):
        role_row = conn.execute(
            sa.text("SELECT id FROM roles WHERE name = :name AND organization_id IS NULL"),
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
            {"role_id": role_row[0], "permission_id": actual_perm_id},
        )


def downgrade() -> None:
    # Reverse in dependency order.  Note: the seeded permission row and its
    # role_permissions grants are intentionally left in place on downgrade
    # (same retention policy migration 002's downgrade applies to its seeds —
    # permission data is not tenant data and removing it could orphan audits).
    op.drop_constraint(
        "ck_processing_jobs_version_required", "processing_jobs", type_="check"
    )
    op.drop_column("processing_jobs", "checkpoint")
    # Org-wide scan jobs have no version anchor — remove them before the
    # column's NOT NULL constraint is restored.
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM processing_jobs WHERE job_type = 'CONFLICT_SCAN'"))
    op.alter_column(
        "processing_jobs",
        "document_version_id",
        existing_type=UUID(as_uuid=False),
        nullable=False,
    )

    op.drop_index(
        "ix_conflict_statements_document_version_id", table_name="conflict_statements"
    )
    op.drop_index("ix_conflict_statements_chunk_id", table_name="conflict_statements")
    op.drop_index(
        "ix_conflict_statements_conflict_id", table_name="conflict_statements"
    )
    op.drop_table("conflict_statements")

    op.drop_index("ix_conflicts_org_detected", table_name="conflicts")
    op.drop_index("ix_conflicts_org_status", table_name="conflicts")
    op.drop_table("conflicts")
