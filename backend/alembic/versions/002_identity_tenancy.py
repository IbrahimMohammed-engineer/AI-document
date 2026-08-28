"""Migration 002 — Identity & tenancy core tables.

Creates the following tables:
  - organizations    (tenant root)
  - roles            (system + org-defined roles)
  - permissions      (fixed catalog, code-owned)
  - role_permissions (N:M roles ↔ permissions)
  - users            (tenant-scoped users)
  - user_roles       (N:M users ↔ roles)
  - refresh_tokens   (server-side revocable refresh token store)

Also seeds the system roles (Admin, Editor, Viewer) and the fixed
permissions catalog as part of this migration, so they are always present
after `alembic upgrade head`.

See Database-Architecture-Design-Documentation.md §11–12.

Revision ID: 002
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── organizations ─────────────────────────────────────────────────────────
    op.create_table(
        "organizations",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("plan", sa.String(50), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("settings", JSONB(), nullable=False, server_default=sa.text("'{}'")),
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
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    # ── roles ─────────────────────────────────────────────────────────────────
    op.create_table(
        "roles",
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
            nullable=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_roles_organization_id", "roles", ["organization_id"])
    # Standard UNIQUE treats NULLs as distinct — add a partial unique index for system roles
    # (organization_id IS NULL) so duplicate system role names are prevented.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_roles_system_name
        ON roles (name)
        WHERE organization_id IS NULL
        """
    )
    op.create_unique_constraint(
        "uq_roles_org_name",
        "roles",
        ["organization_id", "name"],
    )

    # ── permissions ───────────────────────────────────────────────────────────
    op.create_table(
        "permissions",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
    )

    # ── role_permissions ──────────────────────────────────────────────────────
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            UUID(as_uuid=False),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "permission_id",
            UUID(as_uuid=False),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ── users ─────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
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
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "last_login_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_unique_constraint(
        "uq_users_org_email", "users", ["organization_id", "email"]
    )
    op.create_index("ix_users_organization_id", "users", ["organization_id"])
    # Partial index for soft-delete-aware queries
    op.execute(
        """
        CREATE INDEX ix_users_org_active
        ON users (organization_id)
        WHERE deleted_at IS NULL
        """
    )

    # ── user_roles ────────────────────────────────────────────────────────────
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            UUID(as_uuid=False),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_user_roles_organization_id", "user_roles", ["organization_id"])

    # ── refresh_tokens ────────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    # ── Seed permissions catalog (fixed, code-owned) ──────────────────────────
    _seed_permissions()

    # ── Seed system roles (Admin, Editor, Viewer) ─────────────────────────────
    _seed_system_roles()


def _seed_permissions() -> None:
    """Insert the fixed permission catalog.

    These keys are referenced by name in application code — they must always
    exist after any `alembic upgrade head`.
    """
    permissions = [
        ("document:create", "Create new documents and upload files"),
        ("document:read", "View documents, versions, and download files"),
        ("document:update", "Edit document metadata and upload new versions"),
        ("document:delete", "Soft-delete documents"),
        ("chat:create", "Start conversations and ask questions"),
        ("comparison:create", "Run document comparisons"),
        ("user:manage", "Invite, deactivate, and manage users and their roles"),
        ("settings:manage", "Modify organization settings and integrations"),
        ("analytics:read", "View usage analytics and audit logs"),
    ]
    conn = op.get_bind()
    for key, description in permissions:
        perm_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO permissions (id, key, description) "
                "VALUES (:id, :key, :description) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"id": perm_id, "key": key, "description": description},
        )


def _seed_system_roles() -> None:
    """Insert the three built-in system roles and wire their permissions.

    System roles have organization_id = NULL and is_system = TRUE.
    They are protected from deletion at the application layer.

    Permission assignments:
      Admin  — all permissions
      Editor — document:*, chat:create, comparison:create, analytics:read
      Viewer — document:read, chat:create
    """
    conn = op.get_bind()

    # Resolve permission IDs
    rows = conn.execute(sa.text("SELECT id, key FROM permissions")).fetchall()
    perm_map = {row[1]: row[0] for row in rows}

    role_permissions = {
        "Admin": list(perm_map.keys()),
        "Editor": [
            "document:create",
            "document:read",
            "document:update",
            "document:delete",
            "chat:create",
            "comparison:create",
            "analytics:read",
        ],
        "Viewer": [
            "document:read",
            "chat:create",
        ],
    }

    for role_name, perm_keys in role_permissions.items():
        role_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO roles (id, name, is_system) "
                "VALUES (:id, :name, true) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": role_id, "name": role_name},
        )
        # Re-fetch the role id (in case ON CONFLICT hit an existing row)
        row = conn.execute(
            sa.text(
                "SELECT id FROM roles WHERE name = :name AND organization_id IS NULL"
            ),
            {"name": role_name},
        ).fetchone()
        if row is None:
            continue
        actual_role_id = row[0]

        for perm_key in perm_keys:
            perm_id = perm_map.get(perm_key)
            if perm_id:
                conn.execute(
                    sa.text(
                        "INSERT INTO role_permissions (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"role_id": actual_role_id, "permission_id": perm_id},
                )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("organizations")
