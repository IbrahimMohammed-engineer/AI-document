"""
Alembic async migration environment.

This env.py configures Alembic to:
  - Use the SYNC database URL (psycopg2) because Alembic does not natively
    support async connections.
  - Auto-detect all models from app.models (import them all before running
    autogenerate so Alembic sees the full schema).
  - Apply migrations in both online mode (against a real DB) and offline mode
    (generate SQL scripts without a DB connection).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Import all models so Alembic autogenerate detects them
from app.models.base import Base  # noqa: F401 — sets up Base.metadata
from app.models.organization import Organization  # noqa: F401
from app.models.user import (  # noqa: F401
    AuditLog,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)
# Phase 3 — document management models
from app.models.document import (  # noqa: F401
    Collection,
    CollectionDocument,
    Document,
    DocumentTag,
    DocumentVersion,
)

# ─── Alembic Config ───────────────────────────────────────────────────────────

config = context.config

# Set the SQLAlchemy URL from the environment (overrides alembic.ini value)
db_url = os.getenv(
    "MIGRATION_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://aidoc_user:aidoc_password@localhost:5432/aidoc_db",
    ),
)
# Alembic uses a sync driver; replace asyncpg with psycopg2 if needed
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "+psycopg2")

config.set_main_option("sqlalchemy.url", db_url)

# Logging config from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object that Alembic uses for autogenerate
target_metadata = Base.metadata


# ─── Offline mode ─────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """Render migration SQL without a DB connection.

    Useful for generating a migration script to be reviewed/applied manually.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ─── Online mode ──────────────────────────────────────────────────────────────

def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


# ─── Entry point ──────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
