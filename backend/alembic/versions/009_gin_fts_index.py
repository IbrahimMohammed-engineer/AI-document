"""
Migration 009 — GIN full-text-search index + filter-supporting indexes (Phase 8: Hybrid Search and Reranking).

What this migration does:
  1. Adds the GIN index on ``document_chunks.content_tsv`` — the generated
     ``tsvector`` column created by migration 007 (``GENERATED ALWAYS ... STORED``).
     This is the full-text branch's workhorse index (DB §18): without it every
     ``content_tsv @@ websearch_to_tsquery(...)`` query degrades to a sequential
     scan at production volumes.

     Built CONCURRENTLY (safe on a live system with existing data) — the same
     pattern as migration 008's HNSW index, for the same reason.

  2. Adds supporting relational-filter indexes verified per DB §27 so the
     planner can intersect the metadata predicates with the FTS/vector scans
     efficiently rather than sequentially scanning a large filtered set:
       - ``ix_documents_org_department``  (organization_id, department)
         — department is a Phase 8 metadata filter dimension (Backend §30).
       - ``ix_document_versions_effective_date`` (document_id, effective_date)
         — temporal/effective-date version resolution
         (``resolve_current_version(as_of=…)`` reuses this per document).

     Existing indexes already cover the other filter dimensions:
       documents.document_type   → ix_documents_org_type (Phase 3)
       documents.owner_id        → ix_documents_owner_id (Phase 1)
       collections N:M           → collection_documents PK (Phase 3)

Model/dimension decision note: no new columns here — content_tsv was created
by migration 007 with the 'english' text-search configuration pinned in the
GENERATED column expression.  English-only FTS in V1 is a documented,
acceptable limitation (DB §40 item 7 — multi-language is future evolution);
changing the configuration requires regenerating the column via a NEW
migration — this migration must never be edited.

Revision ID: 009
Depends on:  008
"""
from __future__ import annotations

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # CONCURRENTLY cannot run inside a transaction block — exit Alembic's
    # implicit transaction first (same approach as migration 008).
    op.execute("COMMIT")

    # ── GIN index on the generated tsvector column (DB §18) ───────────────
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_chunks_content_tsv_gin
        ON document_chunks
        USING gin (content_tsv)
        """
    )
    op.execute(
        """
        COMMENT ON INDEX ix_document_chunks_content_tsv_gin IS
        'GIN index for full-text keyword search over the content_tsv generated
         column (Phase 8 hybrid search). Queried with
         websearch_to_tsquery english (query text as a bound parameter, never
         string-concatenated - Backend 52). English config is pinned by the
         generated column expression (migration 007).'
        """
    )

    # ── Supporting relational-filter indexes (DB §27) ──────────────────────
    op.create_index(
        "ix_documents_org_department",
        "documents",
        ["organization_id", "department"],
    )
    op.create_index(
        "ix_document_versions_effective_date",
        "document_versions",
        ["document_id", "effective_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_versions_effective_date", table_name="document_versions"
    )
    op.drop_index("ix_documents_org_department", table_name="documents")
    op.execute("COMMIT")  # exit transaction for CONCURRENTLY
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_document_chunks_content_tsv_gin"
    )
