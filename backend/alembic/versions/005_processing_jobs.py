"""
Migration 005 — Processing Jobs (Phase 4: Background Processing Infrastructure).

Creates the durable job record table:

    processing_jobs — authoritative per-job state (PostgreSQL is the system of
    record; Redis/Arq entries are disposable pointers re-derivable from these
    rows via the reconciliation sweep).

Columns per roadmap Phase 4 step 1:
    id, organization_id, document_version_id, job_type,
    status (PENDING/PROCESSING/RETRYING/COMPLETED/FAILED),
    attempts, max_attempts, error_message, progress (+ progress_message),
    started_at, completed_at, created_at, updated_at

Indexes: (document_version_id, status) and (status, created_at).

The job_type CHECK includes the full set of known pipeline/maintenance job
types (extraction stages, comparison, summary, conflict scan, purge) so later
phases never need a migration just to introduce a new stage — they only add
handlers.

See:
  Backend-Architecture-Documentation.md §23 (Background Processing Architecture)
  Backend-Architecture-Documentation.md §47 (State Machines)
  Database-Architecture-Design-Documentation.md §10 (ERD: document_versions 1───N processing_jobs)

Revision ID: 005
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | None = None
depends_on: str | None = None

# Known job types across the whole platform (closed set — handlers arrive per phase):
#   EXTRACTION / OCR / CHUNKING / EMBEDDING / INDEXING  — ingestion stages (Phases 4–7)
#   COMPARISON / SUMMARY / CONFLICT_SCAN                — advanced intelligence (Phases 12–14)
#   PURGE                                               — retention/maintenance (Phases 16/20)
JOB_TYPES = (
    "EXTRACTION", "OCR", "CHUNKING", "EMBEDDING", "INDEXING",
    "COMPARISON", "SUMMARY", "CONFLICT_SCAN", "PURGE",
)

# Job lifecycle states (Backend §47 — RETRYING is a distinct, visible state)
JOB_STATUSES = ("PENDING", "PROCESSING", "RETRYING", "COMPLETED", "FAILED")


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
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
            comment="Tenant owner — carried explicitly in job payloads and re-validated by workers",
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
            comment="The version this job processes — the worker loads it fresh, never trusts a payload snapshot",
        ),
        sa.Column(
            "job_type",
            sa.String(30),
            nullable=False,
            comment="Pipeline stage or maintenance task: " + " | ".join(JOB_TYPES),
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="PENDING",
            comment="PENDING | PROCESSING | RETRYING | COMPLETED | FAILED",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Number of execution attempts already made",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="3",
            comment="Retry budget for this job (per-job-type policy; 1 = deterministic stage)",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Last failure reason (typed, metadata only — never document content)",
        ),
        sa.Column(
            "progress",
            sa.SmallInteger(),
            nullable=True,
            comment="0–100 completion percentage written incrementally by workers",
        ),
        sa.Column(
            "progress_message",
            sa.Text(),
            nullable=True,
            comment='Human-readable progress detail, e.g. "340/512 chunks embedded"',
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="First claim time (set once; retries keep the original)",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Terminal transition time (COMPLETED or FAILED)",
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
        # CHECK constraints
        sa.CheckConstraint(
            f"job_type IN ({', '.join(repr(t) for t in JOB_TYPES)})",
            name="ck_processing_jobs_job_type",
        ),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in JOB_STATUSES)})",
            name="ck_processing_jobs_status",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)",
            name="ck_processing_jobs_progress_range",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1",
            name="ck_processing_jobs_attempts",
        ),
    )

    # Indexes per roadmap Phase 4 step 1
    op.create_index(
        "ix_processing_jobs_document_version_id",
        "processing_jobs",
        ["document_version_id", "status"],
    )
    op.create_index(
        "ix_processing_jobs_status",
        "processing_jobs",
        ["status", "created_at"],
    )
    # Active-job listing for an org (header processing indicator widget)
    op.create_index(
        "ix_processing_jobs_org_status",
        "processing_jobs",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_org_status", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_status", table_name="processing_jobs")
    op.drop_index(
        "ix_processing_jobs_document_version_id", table_name="processing_jobs"
    )
    op.drop_table("processing_jobs")
