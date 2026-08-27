"""
User, Role, Permission, and auth-related ORM models.

Maps to tables defined in Database-Architecture-Design-Documentation.md §11:
  - users
  - roles
  - permissions
  - user_roles  (N:M join)
  - role_permissions (N:M join)
  - refresh_tokens
  - audit_logs
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from app.models.organization import Organization


# ─── Roles ────────────────────────────────────────────────────────────────────

class Role(Base):
    """System-defined or org-defined roles.

    System roles (Admin, Editor, Viewer) have organization_id = NULL and
    is_system = True. Custom org roles have a non-null organization_id.

    See Database-Architecture-Design-Documentation.md §11.
    """

    __tablename__ = "roles"
    __table_args__ = (
        # Standard UNIQUE treats NULLs as distinct, so system roles need a
        # partial unique index — the migration handles this; the model just
        # documents the intent.
        UniqueConstraint("organization_id", "name", name="uq_roles_org_name"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="NULL = system role shared across all orgs",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True for built-in Admin/Editor/Viewer — protected from deletion",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    permissions: Mapped[list[Permission]] = relationship(
        "Permission",
        secondary="role_permissions",
        back_populates="roles",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<Role id={self.id!r} name={self.name!r} is_system={self.is_system}>"


# ─── Permissions ──────────────────────────────────────────────────────────────

class Permission(Base):
    """Fixed, code-owned permission catalog.

    Permission keys are seeded via migration and are not user-editable —
    only their assignment to roles is configurable.

    See Database-Architecture-Design-Documentation.md §11.
    """

    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    key: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        comment=(
            "e.g. document:create, document:read, document:update, document:delete, "
            "chat:create, comparison:create, user:manage, settings:manage, analytics:read"
        ),
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    roles: Mapped[list[Role]] = relationship(
        "Role",
        secondary="role_permissions",
        back_populates="permissions",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<Permission key={self.key!r}>"


# ─── N:M join tables ──────────────────────────────────────────────────────────

class RolePermission(Base):
    """N:M join table — roles ↔ permissions.

    See Database-Architecture-Design-Documentation.md §11.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserRole(Base):
    """N:M join table — users ↔ roles.

    organization_id is denormalized from the user for direct index-friendly
    tenant filtering on role lookups (avoids an extra join on every permission
    check, which happens on nearly every request).

    See Database-Architecture-Design-Documentation.md §11.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        Index("ix_user_roles_organization_id", "organization_id"),
    )

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        comment="Denormalized from user — enables efficient tenant-scoped role lookups",
    )


# ─── User ─────────────────────────────────────────────────────────────────────

class User(Base, TimestampMixin):
    """Platform user, always a member of exactly one organization (tenant).

    See Database-Architecture-Design-Documentation.md §11.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        Index("ix_users_organization_id", "organization_id"),
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
    )
    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        comment="Unique within the organization (not globally)",
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Argon2 hash. NULL for SSO-only users.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft delete — users are never hard-deleted while references exist",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    organization: Mapped[Organization] = relationship(
        "Organization",
        back_populates="users",
        foreign_keys=[organization_id],
        lazy="noload",
    )
    roles: Mapped[list[Role]] = relationship(
        "Role",
        secondary="user_roles",
        lazy="noload",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} email={self.email!r}>"


# ─── Refresh Token ────────────────────────────────────────────────────────────

class RefreshToken(Base):
    """Server-side revocable refresh token store.

    Refresh tokens are opaque random strings (not JWTs) stored server-side
    so they can be individually revoked. Rotation on use: the old token is
    invalidated on every use and a new one issued — theft detection via reuse.

    See Backend-Architecture-Documentation.md §12.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_token_hash", "token_hash"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="SHA-256 of the opaque refresh token (never store raw tokens)",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set when the token is rotated or explicitly revoked (logout)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="refresh_tokens", lazy="noload")

    @property
    def is_valid(self) -> bool:
        """Return True if the token has not been revoked and has not expired."""
        from datetime import datetime, timezone
        return (
            self.revoked_at is None
            and self.expires_at > datetime.now(tz=timezone.utc)
        )

    def __repr__(self) -> str:
        return f"<RefreshToken id={self.id!r} user_id={self.user_id!r}>"


# ─── Audit Log ────────────────────────────────────────────────────────────────

class AuditLog(Base):
    """Insert-only audit trail for all significant actions.

    IMPORTANT: The application role has UPDATE/DELETE revoked on this table
    at the database level (enforced by migration 003). Use AuditLogger utility
    (Phase 2+) to write events — never update or delete rows directly.

    resource_id is deliberately NOT a foreign key so audit records survive
    even after the referenced resource is hard-deleted.

    See Database-Architecture-Design-Documentation.md §24.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index(
            "ix_audit_logs_org_resource",
            "organization_id",
            "resource_type",
            "resource_id",
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
        ForeignKey("organizations.id"),
        nullable=False,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id"),
        nullable=True,
        comment="NULL for system-initiated actions",
    )
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment=(
            "e.g. USER_LOGIN, DOCUMENT_UPLOADED, DOCUMENT_VIEWED, "
            "DOCUMENT_DELETED, QUESTION_ASKED, ACCESS_LEVEL_CHANGED"
        ),
    )
    resource_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="e.g. document, conversation, user, comparison",
    )
    resource_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        nullable=True,
        comment=(
            "Intentionally NOT a FK — audit records must survive resource deletion. "
            "See Database-Architecture-Design-Documentation.md §24."
        ),
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Action-specific context",
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog action={self.action!r} "
            f"resource={self.resource_type}/{self.resource_id}>"
        )
