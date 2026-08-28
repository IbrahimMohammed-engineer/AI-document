"""
Migration 006 — Document Pages (Phase 5: Document Extraction and OCR).

Creates the page-level extraction store:

    document_pages — one row per extracted/OCR'd page of a version
    (id, document_version_id, page_number, text, ocr_used,
     render_storage_key, width, height, metadata, created_at)

Unique: (document_version_id, page_number) — the idempotency anchor for
streamed, resumable extraction (re-runs never blind re-INSERT; the worker
resumes from the last persisted page, Backend §49).

The `metadata` JSONB carries the per-page OCR provenance: provider name,
confidence (internal quality signal only — never user-facing in V1),
size-bounded per-line bounding boxes for future citation highlighting, and
the explicit "ocr_failed" marker that keeps a single page's OCR failure from
failing the whole version (Backend §19).

No new columns on document_versions — `page_count` already exists (004).

See:
  Database-Architecture-Design-Documentation.md §15 (Page and Section Model)
  Database-Architecture-Design-Documentation.md §27 (Index Strategy)
  Backend-Architecture-Documentation.md §18 (File Extraction), §19 (OCR)

Revision ID: 006
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "document_pages",
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
            "page_number",
            sa.Integer(),
            nullable=False,
            comment="1-indexed reading order",
        ),
        sa.Column(
            "text",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Extracted or OCR'd raw text for the page",
        ),
        sa.Column(
            "ocr_used",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True if this page's text came from OCR rather than native extraction",
        ),
        sa.Column(
            "render_storage_key",
            sa.Text(),
            nullable=True,
            comment="Optional pre-rendered page image in object storage (later optimization)",
        ),
        sa.Column(
            "width",
            sa.Numeric(10, 2),
            nullable=True,
            comment="Page width in points — normalizes highlight bounding boxes",
        ),
        sa.Column(
            "height",
            sa.Numeric(10, 2),
            nullable=True,
            comment="Page height in points — normalizes highlight bounding boxes",
        ),
        sa.Column(
            "metadata",
            JSONB(),
            nullable=True,
            comment="OCR provenance: provider, confidence (internal), line bounding boxes, ocr_failed marker",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "page_number >= 1",
            name="ck_document_pages_page_number",
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "page_number",
            name="uq_document_pages_version_page",
        ),
    )

    # (document_version_id, page_number) lookups are covered by the unique
    # constraint's index (leftmost prefix = version filter). No extra index
    # needed per DB §27 for this access pattern.


def downgrade() -> None:
    op.drop_table("document_pages")
