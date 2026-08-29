"""
Migration 010 — messages + citations tables (Phase 10: Citations and Source Validation).

What this migration does (roadmap Phase 10 step 3; DB §21–22):

  1. ``messages`` (DB §21) — the chat-message store.  Phase 10 lands the table
     because citations are written ATOMICALLY with the assistant message
     (Backend §50): an assistant message with citation markers but no
     ``citations`` rows (or vice versa) is structurally impossible.

     Schema note: ``conversation_id`` is NULLABLE here WITHOUT a foreign key —
     the ``conversations`` table itself is a Phase 11 migration; its migration
     adds the FK and tightens the column to NOT NULL.  Phase 10 writes
     standalone assistant answers (conversation_id NULL); Phase 11 attaches
     conversations.

     Assistant-only columns (``model``, ``prompt_tokens``, ``completion_tokens``,
     ``retrieval_ms``, ``latency_ms``, ``groundedness``) are nullable and CHECK-
     constrained to stay NULL on non-ASSISTANT rows (DB §21 — one wide table,
     no 1:1 side table: the dominant read is "all messages for a conversation").

  2. ``citations`` (DB §22) — the single most important table for the
     explainability principle: every AI claim traces through this table to
     exact source evidence.  Deliberately DENORMALIZED (``document_id``,
     ``page_number``, ``section`` copied from the normalized chain) because
     citations render on the hottest read path in the product (every badge,
     every hover) — the normalized FKs are retained for integrity and reverse
     walks ("what was this passage ever cited for").

     FK policy (roadmap Phase 10 step 3): RESTRICT to chunk/page/version —
     cited evidence cannot be silently deleted out from under an answer; the
     FK-nulling purge path is a Phase 16 concern.

  Indexes: ``citations(message_id)`` (the render-path read) and
  ``citations(chunk_id)`` (reverse lookup — feeds Phase 13 conflict evidence
  reuse and audit walks).  ``messages(conversation_id, created_at)`` per DB §27
  (the conversation-history read; harmless while conversation_id is NULL).

Revision ID: 010
Depends on:  009
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── messages (DB §21) ─────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        # FK + NOT NULL arrive with the Phase 11 conversations migration
        sa.Column("conversation_id", pg.UUID(as_uuid=False), nullable=True,
                  comment="Owning conversation (Phase 11 wires the FK; NULL for standalone answers)"),
        sa.Column(
            "role", sa.Text(), nullable=False,
            comment="USER | ASSISTANT | SYSTEM",
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True,
                  comment="ASSISTANT only — LLM model/version used"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True,
                  comment="ASSISTANT only — cost/usage analytics"),
        sa.Column("completion_tokens", sa.Integer(), nullable=True,
                  comment="ASSISTANT only"),
        sa.Column("retrieval_ms", sa.Integer(), nullable=True,
                  comment="ASSISTANT only — retrieval latency (Analytics breakdown)"),
        sa.Column("latency_ms", sa.Integer(), nullable=True,
                  comment="ASSISTANT only — end-to-end response latency"),
        sa.Column(
            "groundedness", sa.Text(), nullable=True,
            comment="ASSISTANT only — grounded | partial | ungrounded",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('USER','ASSISTANT','SYSTEM')",
            name="ck_messages_role",
        ),
        sa.CheckConstraint(
            "groundedness IS NULL OR groundedness IN ('grounded','partial','ungrounded')",
            name="ck_messages_groundedness",
        ),
        # Assistant-only metric columns stay NULL on USER/SYSTEM rows (DB §21)
        sa.CheckConstraint(
            "role = 'ASSISTANT' OR (model IS NULL AND prompt_tokens IS NULL "
            "AND completion_tokens IS NULL AND retrieval_ms IS NULL "
            "AND latency_ms IS NULL AND groundedness IS NULL)",
            name="ck_messages_assistant_only_metrics",
        ),
        sa.CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0",
                           name="ck_messages_prompt_tokens"),
        sa.CheckConstraint("completion_tokens IS NULL OR completion_tokens >= 0",
                           name="ck_messages_completion_tokens"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
    )

    # ── citations (DB §22) ────────────────────────────────────────────────────
    op.create_table(
        "citations",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"),
                  nullable=False,
                  comment="Always an ASSISTANT message"),
        sa.Column("citation_index", sa.Integer(), nullable=False,
                  comment="The [1]/[2] ordinal as it appears inline in the message"),
        # ── Denormalized read-path columns (zero-join badge/list rendering) ──
        sa.Column("document_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("documents.id", ondelete="RESTRICT"),
                  nullable=False,
                  comment="Denormalized — direct joins without traversing versions"),
        sa.Column("document_version_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("chunk_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("document_chunks.id", ondelete="RESTRICT"),
                  nullable=False,
                  comment="The exact retrieved chunk this citation is grounded in"),
        sa.Column("page_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("document_pages.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False,
                  comment="Denormalized from document_pages — zero-join rendering"),
        sa.Column("section", sa.Text(), nullable=True,
                  comment="Denormalized section title/path, e.g. '4.2 Regulatory Review'"),
        sa.Column("quoted_text", sa.Text(), nullable=False,
                  comment="The exact source span the claim is based on — real source text, never model output"),
        sa.Column("char_start", sa.Integer(), nullable=True,
                  comment="Offset of quoted_text within the chunk content (highlight overlay)"),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("relevance_score", sa.Numeric(6, 5), nullable=True,
                  comment="Retrieval score at generation time (internal-facing)"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("citation_index >= 1", name="ck_citations_index"),
        sa.CheckConstraint("page_number >= 1", name="ck_citations_page_number"),
        sa.CheckConstraint(
            "char_start IS NULL OR char_start >= 0", name="ck_citations_char_start"
        ),
        sa.CheckConstraint(
            "char_end IS NULL OR char_end >= 0", name="ck_citations_char_end"
        ),
    )
    op.create_index("ix_citations_message_id", "citations", ["message_id"])
    op.create_index("ix_citations_chunk_id", "citations", ["chunk_id"])
    op.create_index("ix_citations_document_id", "citations", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_citations_document_id", table_name="citations")
    op.drop_index("ix_citations_chunk_id", table_name="citations")
    op.drop_index("ix_citations_message_id", table_name="citations")
    op.drop_table("citations")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
