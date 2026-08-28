"""
Migration 007 — Document Sections + Chunks (Phase 6: Structure Detection and Chunking).

Creates the structural/retrieval backbone produced by the CHUNKING stage:

    document_sections — hierarchical table-of-contents tree per version
        (self-referencing adjacency list: arbitrary TOC depth, trivial
        "immediate children" queries for the TOC tree — DB §15)

    document_chunks — the retrieval unit; every chunk retains full
        provenance back to its page(s) and section, which is what makes
        citations precise rather than approximate (DB §16)

Design points carried over verbatim from the design docs:

  - document_chunks.organization_id is DENORMALIZED from the owning
    document (every retrieval query filters on it directly — DB §8/§16).
    The denormalization gap is closed by the enforce_chunk_org_matches_
    document() trigger: org drift is structurally impossible, not merely
    forbidden by convention (DB §28).
  - embedding / embedding_model are created now, NULL until Phase 7 fills
    them (the vector extension was validated by migration 001).
  - content_tsv is a GENERATED ALWAYS … STORED tsvector — present from the
    start; Phase 8 adds the GIN index when keyword search goes live.
  - Unique (document_version_id, chunk_index) is the chunking idempotency
    anchor: re-runs UPSERT (ON CONFLICT … DO UPDATE), never duplicate
    (Backend §49; roadmap Phase 6 step 8).
  - section deletion cascades to a section's subtree (parent_section_id)
    while chunks degrade to "unsectioned" (section_id SET NULL) — a chunk
    must not disappear because only its section metadata was restructured
    (DB §28).
  - Indexes are exactly the Phase-6 B-trees from DB §27; the HNSW (Phase 7)
    and GIN (Phase 8) indexes arrive with the features they serve.

See:
  Database-Architecture-Design-Documentation.md §15 (Page and Section Model)
  Database-Architecture-Design-Documentation.md §16 (Chunking and RAG Data Model)
  Database-Architecture-Design-Documentation.md §27 (Indexing Strategy)
  Database-Architecture-Design-Documentation.md §28 (Constraints — org trigger)
  Backend-Architecture-Documentation.md §20 (Structure Detection), §21 (Chunking)

Revision ID: 007
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | None = None
depends_on: str | None = None

# Embedding dimensionality (DB §17) — PINNED to the platform default
# embedding model (text-embedding-3-small). Changing this requires a new
# column + full re-embed; see DB §17 before touching.
_EMBEDDING_DIMS = 1536


def upgrade() -> None:
    # ── document_sections ─────────────────────────────────────────────────
    op.create_table(
        "document_sections",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning version — structural child, deleted with it",
        ),
        sa.Column(
            "parent_section_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_sections.id", ondelete="CASCADE"),
            nullable=True,
            comment="Self-reference; NULL = top-level section (adjacency list — arbitrary TOC depth)",
        ),
        sa.Column(
            "title",
            sa.Text(),
            nullable=False,
            comment="Heading text (numbering prefix stripped)",
        ),
        sa.Column(
            "section_number",
            sa.Text(),
            nullable=True,
            comment="e.g. '4.2' — text, not numeric: numbering schemes vary (4.2, IV.b, Appendix A)",
        ),
        sa.Column(
            "start_page",
            sa.Integer(),
            nullable=False,
            comment="1-indexed page the heading appears on",
        ),
        sa.Column(
            "end_page",
            sa.Integer(),
            nullable=True,
            comment="Page where the next same-or-higher-level heading begins (NULL = unterminated)",
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            comment="Reading-order index across the whole tree (section numbers alone aren't sortable)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("start_page >= 1", name="ck_document_sections_start_page"),
        sa.CheckConstraint("sort_order >= 0", name="ck_document_sections_sort_order"),
        sa.UniqueConstraint(
            "document_version_id", "sort_order", name="uq_document_sections_version_order"
        ),
    )
    # TOC-tree rendering: "all sections for this version" + "children of this
    # section" — one composite index serves both access patterns (DB §15/§27).
    op.create_index(
        "ix_document_sections_version_parent",
        "document_sections",
        ["document_version_id", "parent_section_id"],
    )

    # ── document_chunks ───────────────────────────────────────────────────
    op.create_table(
        "document_chunks",
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
            comment="DENORMALIZED from the owning document — retrieval filters on it directly; trigger-enforced consistent (DB §8/§28)",
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
            comment="Owning version — structural child, deleted with it",
        ),
        sa.Column(
            "page_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=False,
            comment="The chunk's starting page — citations say 'page N' with certainty",
        ),
        sa.Column(
            "end_page_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_pages.id", ondelete="CASCADE"),
            nullable=True,
            comment="Populated only when a chunk spans multiple pages (page breaks don't force splits)",
        ),
        sa.Column(
            "section_id",
            UUID(as_uuid=False),
            sa.ForeignKey("document_sections.id", ondelete="SET NULL"),
            nullable=True,
            comment="Nullable — preambles and unstructured documents have no section; chunks degrade, never vanish",
        ),
        sa.Column(
            "chunk_index",
            sa.Integer(),
            nullable=False,
            comment="Sequential reading order within the version, 0-based",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="The chunk's text",
        ),
        sa.Column(
            "content_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 hex of content — dedup + cheap change pre-diff for comparison (Phase 12)",
        ),
        sa.Column(
            "token_count",
            sa.Integer(),
            nullable=False,
            comment="Token count for the configured tokenizer — LLM context-budget accounting",
        ),
        sa.Column(
            "metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Structural extras: heading path, table/list flags, bounding boxes, forced-split notes",
        ),
        sa.Column(
            "embedding_model",
            sa.Text(),
            nullable=True,
            comment="e.g. 'text-embedding-3-small' — set by Phase 7; records exactly which model produced the vector",
        ),
        sa.Column(
            "content_tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', content)",
                persisted=True,
            ),
            nullable=True,
            comment="GENERATED full-text search vector — GIN index arrives with Phase 8",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_chunks_chunk_index"),
        sa.CheckConstraint("token_count >= 0", name="ck_document_chunks_token_count"),
        sa.UniqueConstraint(
            "document_version_id",
            "chunk_index",
            name="uq_document_chunks_version_index",
        ),
    )

    # The pgvector column itself — raw DDL (no SQLAlchemy vector type in the
    # migration toolchain; the extension was created by migration 001).
    op.execute(
        f"ALTER TABLE document_chunks ADD COLUMN embedding vector({_EMBEDDING_DIMS})"
    )
    op.execute(
        "COMMENT ON COLUMN document_chunks.embedding IS "
        f"'pgvector({_EMBEDDING_DIMS}) — NULL until the Phase 7 embedding stage fills it'"
    )

    # Phase-6 B-tree indexes per DB §27 (HNSW/GIN arrive with Phase 7/8).
    op.create_index(
        "ix_document_chunks_document_version_id",
        "document_chunks",
        ["document_version_id"],
    )
    op.create_index("ix_document_chunks_page_id", "document_chunks", ["page_id"])
    op.create_index(
        "ix_document_chunks_section_id", "document_chunks", ["section_id"]
    )
    op.create_index(
        "ix_document_chunks_organization_id", "document_chunks", ["organization_id"]
    )

    # ── Tenant-isolation trigger (DB §28) ─────────────────────────────────
    # The single most security-critical column cannot silently become wrong:
    # organization_id must always equal the owning document's organization.
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_chunk_org_matches_document()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.organization_id <> (
            SELECT d.organization_id
            FROM document_versions v JOIN documents d ON d.id = v.document_id
            WHERE v.id = NEW.document_version_id
          ) THEN
            RAISE EXCEPTION 'chunk organization_id does not match owning document organization_id';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_chunk_org_consistency
          BEFORE INSERT OR UPDATE ON document_chunks
          FOR EACH ROW EXECUTE FUNCTION enforce_chunk_org_matches_document()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_chunk_org_consistency ON document_chunks")
    op.execute("DROP FUNCTION IF EXISTS enforce_chunk_org_matches_document()")
    op.drop_table("document_chunks")
    op.drop_table("document_sections")
