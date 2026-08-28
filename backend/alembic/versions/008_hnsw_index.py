"""
Migration 008 — HNSW index on document_chunks.embedding (Phase 7: Embeddings + Vector Search).

What this migration does:
  1. Adds the HNSW index on ``document_chunks.embedding`` (cosine distance).
     The ``vector(1536)`` column was created by migration 007; this migration
     adds the index that the pgvector ANN query planner needs to avoid a
     full sequential scan at production data volumes (DB §17/§27).

     Built CONCURRENTLY so it does not lock the table (safe on a live system
     with existing data; Alembic's op.execute() is used because SQLAlchemy's
     DDL layer does not yet support pgvector HNSW index syntax).

  2. HNSW parameters (DB §17):
     - m=16:              number of connections per layer (higher = better
                           recall + more index size; 16 is the pgvector default
                           and a good starting point).
     - ef_construction=64: build-time quality (higher = slower build, better
                           index; 64 is the pgvector recommended minimum).
     Session-level ef_search is NOT set here — it is injected per query via
     ``SET LOCAL hnsw.ef_search = :value`` (DB §17; roadmap Phase 7 step 10).

  3. An autovacuum note is recorded as a database comment: the chunks table
     receives bulk INSERTs (embedding writes) and benefits from more aggressive
     autovacuum settings in production (DB §36 — operator-applied, not DDL).

Model/dimension decision note (DB §17 — recorded here as required):
  Platform default: OpenAI ``text-embedding-3-small``, 1536 dimensions.
  ``embedding_dimensions = 1536`` is set in ``Settings`` and enforced by the
  ``EmbeddingDimensionError`` check in ``infrastructure/embeddings.py``.
  Changing the model requires a NEW column (or full re-embed into the existing
  column after dropping the index) — this migration must never be edited.

Revision ID: 008
Depends on:  007  (the embedding column exists; HNSW references it)
"""
from __future__ import annotations

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── HNSW index (pgvector) ──────────────────────────────────────────────
    # CONCURRENTLY: does not acquire a full table lock.
    # Cannot be inside a transaction block — op.execute() runs outside
    # Alembic's implicit transaction when using CREATE INDEX CONCURRENTLY.
    #
    # NOTE: Alembic by default wraps migrations in a transaction.  For
    # CONCURRENTLY to work, the connection's transaction must be committed
    # first.  We do this via connection.execute("COMMIT") before the index
    # creation.  The alternative (in alembic env.py) is to set
    # ``transaction_per_migration = False``; using the inline COMMIT approach
    # keeps this self-contained.

    op.execute("COMMIT")  # exit the implicit migration transaction

    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # Record the index parameters and model/dim decision for operators.
    op.execute(
        """
        COMMENT ON INDEX ix_document_chunks_embedding_hnsw IS
        'HNSW index for pgvector cosine-similarity ANN queries (Phase 7).
         Parameters: m=16, ef_construction=64.
         Model: text-embedding-3-small, 1536 dims.
         ef_search is tuned at query time via SET LOCAL hnsw.ef_search.
         DO NOT change the embedding model without a new migration (DB §17).'
        """
    )

    # ── Autovacuum hint (recorded as a table comment, not DDL) ────────────
    # In production, apply: ALTER TABLE document_chunks SET (
    #   autovacuum_vacuum_scale_factor = 0.01,
    #   autovacuum_analyze_scale_factor = 0.005
    # );
    # This migration does NOT apply it automatically — it is safe as an
    # operator action and avoids contention here (DB §36).
    op.execute(
        """
        COMMENT ON TABLE document_chunks IS
        'Retrieval units (Phase 6+). Phase 7 adds the HNSW embedding index.
         Production note: tune autovacuum_vacuum_scale_factor=0.01 on this
         table (high write volume from batch embedding stage — DB §36).'
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")  # exit transaction for CONCURRENTLY
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_document_chunks_embedding_hnsw"
    )
