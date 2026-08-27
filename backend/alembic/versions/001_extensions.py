"""Migration 001 — PostgreSQL extensions.

Creates the extensions required by the platform:
  - pgcrypto: provides gen_random_uuid() for UUID primary keys
  - vector: pgvector extension for semantic similarity search (Phase 7)
            Creating it here validates that the pgvector Docker image is correct.

Revision ID: 001
"""
from __future__ import annotations

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # NOTE: Dropping extensions in production is destructive — only done in dev/CI.
    # The vector extension cannot be dropped if any column uses the vector type.
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
