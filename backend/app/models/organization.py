"""
Organization ORM model.

Maps to the `organizations` table defined in Database-Architecture-Design-Documentation.md §12.
The organization is the tenant root — every tenant-owned table has a FK to this.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from app.models.user import User


class Organization(Base, TimestampMixin):
    """Tenant root entity.

    See Database-Architecture-Design-Documentation.md §12.
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=generate_uuid,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="URL-safe identifier for SSO routing / subdomain resolution",
    )
    plan: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="standard",
        server_default="standard",
        comment="Subscription tier: standard | enterprise | ...",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="active",
        server_default="active",
        comment="active | suspended",
    )
    settings: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Org-level config: AI settings, retention policy, etc.",
    )

    # ─── Relationships ────────────────────────────────────────────────────────
    users: Mapped[list[User]] = relationship(
        "User",
        back_populates="organization",
        foreign_keys="User.organization_id",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<Organization id={self.id!r} slug={self.slug!r}>"
