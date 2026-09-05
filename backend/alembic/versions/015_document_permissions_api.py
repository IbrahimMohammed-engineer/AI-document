"""Migration 015 — Phase 16 security hardening.

Creates:
  - document_permissions — explicit per-user access grants backing the
    RESTRICTED access level (owner + explicit grants; every other org
    member is denied). NOTE: this table was referenced by the Phase 7/16
    design docs but had never been created by any prior migration — the
    DDL lands here together with the application-layer enforcement.

Seeds:
  - document:admin permission ("Manage explicit access grants on
    restricted documents"), granted to the Admin system role (mirrors the
    migration 002 catalog pattern; Viewer/Editor deliberately excluded —
    grant management is an administrative action).

Revision ID: 015
Revises: 014
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic
revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── document_permissions ──────────────────────────────────────────────────
    op.create_table(
        "document_permissions",
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
            comment="DENORMALIZED tenant scope — grants never cross organizations",
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=False),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="Grantee — must belong to the same organization",
        ),
        sa.Column(
            "permission_type",
            sa.String(20),
            nullable=False,
            server_default="read",
            comment="read | write | admin",
        ),
        sa.Column(
            "granted_by",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="The owner/admin who created the grant",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="NULL = never expires; past value = grant dormant",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "permission_type IN ('read','write','admin')",
            name="ck_document_permissions_type",
        ),
        sa.UniqueConstraint(
            "document_id", "user_id", name="uq_document_permissions_doc_user"
        ),
    )
    # The dominant lookup: resolve_allowed_documents' grant check
    op.create_index(
        "ix_document_permissions_lookup",
        "document_permissions",
        ["document_id", "user_id"],
    )
    op.create_index(
        "ix_document_permissions_organization_id",
        "document_permissions",
        ["organization_id"],
    )

    _seed_document_admin_permission()


def _seed_document_admin_permission() -> None:
    """Seed document:admin; grant it to the Admin system role.

    Mirrors the migration 002/014 catalog pattern exactly (raw SQL
    INSERT ... ON CONFLICT DO NOTHING).
    """
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO permissions (id, key, description) "
            "VALUES (:id, :key, :description) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "key": "document:admin",
            "description": (
                "Manage explicit access grants on restricted documents"
            ),
        },
    )
    row = conn.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": "document:admin"},
    ).fetchone()
    if row is None:
        return
    permission_id = row[0]
    role_row = conn.execute(
        sa.text(
            "SELECT id FROM roles WHERE name = :name AND organization_id IS NULL"
        ),
        {"name": "Admin"},
    ).fetchone()
    if role_row is None:
        return
    conn.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "VALUES (:role_id, :permission_id) "
            "ON CONFLICT DO NOTHING"
        ),
        {"role_id": role_row[0], "permission_id": permission_id},
    )


def downgrade() -> None:
    # Reverse in dependency order.  The seeded permission row and its
    # role_permissions grant are intentionally left in place on downgrade
    # (same retention policy as migrations 002/013/014 — permission data is
    # not tenant data and removing it could orphan audit rows).
    op.drop_index(
        "ix_document_permissions_organization_id", table_name="document_permissions"
    )
    op.drop_index(
        "ix_document_permissions_lookup", table_name="document_permissions"
    )
    op.drop_table("document_permissions")
