"""
Migration 011 — conversations + conversation_documents + message_feedback
(Phase 11: Conversations and Streaming Chat).

What this migration does (roadmap Phase 11 step 1; DB §20–21, §27):

  1. ``conversations`` (DB §20) — the chat container.  Private to the creating
     user in V1 (``user_id`` NOT NULL — the shared/team conversation model is
     a §40 future evolution).  ``scope_type`` mirrors the frontend scope
     selector exactly; scope CHANGES are never silent mutations of this
     column alone — they are recorded as SYSTEM-role ``messages`` markers
     (Backend §46 rule 18) — but the column always reflects the CURRENT
     setting.  ``deleted_at`` is the "archive conversation" soft delete.
     ``updated_at`` is bumped on every new message and drives the
     conversation list's recency ordering.

  2. ``conversation_documents`` (DB §21) — the reproducibility backbone for
     ``scope_type='selected_documents'``: which documents the conversation is
     scoped to.  ``removed_at`` (rather than row deletion) records scope
     shrinkage so "what scope did THIS historical answer use" is always
     reconstructable.  PK ``(conversation_id, document_id)``.

  3. ``message_feedback`` (DB §21) — one rating per user per message
     (UNIQUE constraint); a resubmission UPSERTS rather than duplicating.
     A separate table (not columns on messages) because feedback has its own
     actor + timestamp, independent of the message author.

  4. ``messages``:
     - Foreign key ``conversation_id → conversations(id) ON DELETE CASCADE``
       (messages have no existence outside their conversation — DB §28).
       The column STAYS NULLABLE: the Phase 10 standalone ``POST /ask``
       pipeline (retained for the evaluation harness) persists answers with
       ``conversation_id NULL``.  Conversations themselves are created
       lazily on first message send (DB §20 lifecycle rule).
     - ``metadata`` JSONB NULL — per-message generation metadata beyond the
       fixed metric columns; Phase 11 writes ``{"stopped": true}`` when an
       answer was frozen mid-generation by the stop control (roadmap Phase 11
       step 6).  Plain JSONB (not JSON) so the value is stored compactly.

  Indexes (DB §27):
     - ``conversations (organization_id, user_id, updated_at DESC)`` — the
       conversation list: recency-ordered, org+user scoped.
     - ``conversation_documents (document_id)`` — reverse walks ("which
       conversations reference this document", purge-time checks).

Revision ID: 011
Depends on:  010
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── conversations (DB §20) ────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False,
                  comment="Owner — conversations are private to the creating user in V1"),
        sa.Column("title", sa.Text(), nullable=True,
                  comment="Auto-generated from the first message when not user-set"),
        sa.Column("scope_type", sa.Text(), nullable=False,
                  server_default="knowledge_base",
                  comment="current_document | selected_documents | knowledge_base"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(),
                  comment="Bumped on every new message — recency ordering"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True,
                  comment="Soft delete — archive conversation"),
        sa.CheckConstraint(
            "scope_type IN ('current_document','selected_documents','knowledge_base')",
            name="ck_conversations_scope_type",
        ),
    )
    op.create_index(
        "ix_conversations_org_user_updated",
        "conversations",
        ["organization_id", "user_id", sa.text("updated_at DESC")],
    )

    # ── conversation_documents (DB §21) ───────────────────────────────────────
    op.create_table(
        "conversation_documents",
        sa.Column("conversation_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("document_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="Set (not deleted) when a document is unchecked mid-conversation"),
        sa.PrimaryKeyConstraint("conversation_id", "document_id",
                                name="pk_conversation_documents"),
    )
    op.create_index(
        "ix_conversation_documents_document_id",
        "conversation_documents",
        ["document_id"],
    )

    # ── message_feedback (DB §21) ─────────────────────────────────────────────
    op.create_table(
        "message_feedback",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False,
                  comment="-1 (negative) / 1 (positive)"),
        sa.Column("comment", sa.Text(), nullable=True,
                  comment="Optional reason (captured on negative feedback)"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_message_feedback_rating"),
        sa.UniqueConstraint("message_id", "user_id",
                            name="uq_message_feedback_message_user"),
    )
    op.create_index(
        "ix_message_feedback_message_id", "message_feedback", ["message_id"]
    )

    # ── messages: attach the conversation FK + metadata column ────────────────
    op.create_foreign_key(
        "fk_messages_conversation_id",
        "messages",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "messages",
        sa.Column("metadata", pg.JSONB(), nullable=True, server_default=sa.text("'{}'"),
                  comment="Generation metadata (e.g. {\"stopped\": true} on stop-frozen answers)"),
    )


def downgrade() -> None:
    op.drop_column("messages", "metadata")
    op.drop_constraint("fk_messages_conversation_id", "messages", type_="foreignkey")
    op.drop_index("ix_message_feedback_message_id", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_conversation_documents_document_id", table_name="conversation_documents")
    op.drop_table("conversation_documents")
    op.drop_index("ix_conversations_org_user_updated", table_name="conversations")
    op.drop_table("conversations")
