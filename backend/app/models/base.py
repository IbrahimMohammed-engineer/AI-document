"""
SQLAlchemy declarative base for all ORM models.

All models inherit from `Base`. The `TimestampMixin` provides `created_at`
and `updated_at` columns following the schema in Database-Architecture-Design-Documentation.md.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


class TimestampMixin:
    """Provides `created_at` and `updated_at` columns.

    - `created_at` is set once at INSERT by the database DEFAULT.
    - `updated_at` is updated at every UPDATE by the `onupdate` trigger.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def generate_uuid() -> str:
    """Generate a new UUID string (used as Python-side default for PK columns)."""
    return str(uuid.uuid4())
