"""Migration 003 — Audit logs table + insert-only grants.

Creates the `audit_logs` table with an insert-only policy:
  - REVOKE UPDATE, DELETE from the application runtime role
  - Only a privileged maintenance role can delete rows (for retention-policy purges)

This table is expected to be the highest-volume table in the schema (every
view, action, and question generates an entry). See Database-Architecture-
Design-Documentation.md §24 for the full design.

Revision ID: 003
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None

# The runtime app role — must match the role used in DATABASE_URL
APP_ROLE = "aidoc_user"


def upgrade() -> None:
    # ── audit_logs ────────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="NULL for system-initiated actions",
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column(
            "resource_id",
            UUID(as_uuid=False),
            nullable=True,
            comment=(
                "Intentionally NOT a foreign key — audit records survive resource deletion. "
                "See Database-Architecture-Design-Documentation.md §24."
            ),
        ),
        sa.Column(
            "metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Indexes — see Database-Architecture-Design-Documentation.md §27
    op.create_index(
        "ix_audit_logs_org_created",
        "audit_logs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_org_resource",
        "audit_logs",
        ["organization_id", "resource_type", "resource_id"],
    )

    # ── Insert-only grants ────────────────────────────────────────────────────
    # The app runtime role may INSERT but NEVER UPDATE or DELETE audit_logs rows.
    # This enforces the audit log's immutability guarantee at the database level.
    # A compromised application process cannot rewrite history.
    #
    # Only applied when the role exists (e.g. the Docker/development database
    # provisions aidoc_user via init_db.sql). Test containers connect as the
    # cluster superuser, so the grant is skipped there. NOTE: a failed REVOKE
    # cannot simply be caught — PostgreSQL aborts the whole migration
    # transaction — so the role is checked up front instead.
    conn = op.get_bind()
    role_exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
    ).scalar()
    if role_exists:
        conn.execute(sa.text(f"REVOKE UPDATE, DELETE ON audit_logs FROM {APP_ROLE}"))


def downgrade() -> None:
    # Restore full grants before dropping (for clean down in dev/CI)
    conn = op.get_bind()
    role_exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
    ).scalar()
    if role_exists:
        conn.execute(sa.text(f"GRANT UPDATE, DELETE ON audit_logs TO {APP_ROLE}"))
    op.drop_index("ix_audit_logs_org_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_created", table_name="audit_logs")
    op.drop_table("audit_logs")
