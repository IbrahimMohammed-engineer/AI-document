"""
Integration tests for Alembic migrations.

Tests that:
  - `alembic upgrade head` runs without error (verified by the session fixture)
  - All expected tables are present after upgrade
  - System roles and permissions are seeded correctly
  - `alembic downgrade base` then `upgrade head` is idempotent
"""
from __future__ import annotations


import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
async def test_expected_tables_exist_after_upgrade(async_database_url):
    """After alembic upgrade head, all Phase 1 tables must exist."""
    expected_tables = {
        "organizations",
        "roles",
        "permissions",
        "role_permissions",
        "users",
        "user_roles",
        "refresh_tokens",
        "audit_logs",
    }

    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                """
            )
        )
        actual_tables = {row[0] for row in result.fetchall()}

    await engine.dispose()

    missing = expected_tables - actual_tables
    assert not missing, f"Expected tables missing after migration: {missing}"


@pytest.mark.integration
async def test_system_roles_seeded(async_database_url):
    """System roles Admin, Editor, Viewer must be present after migration."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT name FROM roles WHERE is_system = true ORDER BY name"
            )
        )
        role_names = {row[0] for row in result.fetchall()}

    await engine.dispose()

    assert role_names == {"Admin", "Editor", "Viewer"}, (
        f"Expected system roles {{Admin, Editor, Viewer}}, got: {role_names}"
    )


@pytest.mark.integration
async def test_permissions_catalog_seeded(async_database_url):
    """All 9 permission keys from the catalog must be seeded."""
    expected_keys = {
        "document:create",
        "document:read",
        "document:update",
        "document:delete",
        "chat:create",
        "comparison:create",
        "user:manage",
        "settings:manage",
        "analytics:read",
    }

    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT key FROM permissions"))
        actual_keys = {row[0] for row in result.fetchall()}

    await engine.dispose()

    missing = expected_keys - actual_keys
    assert not missing, f"Permission keys missing: {missing}"


@pytest.mark.integration
async def test_admin_role_has_all_permissions(async_database_url):
    """The Admin system role must have all permissions."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        # Count all permissions
        total = (await conn.execute(sa.text("SELECT count(*) FROM permissions"))).scalar()

        # Count Admin's permissions
        admin_count = (
            await conn.execute(
                sa.text(
                    """
                    SELECT count(*) FROM role_permissions rp
                    JOIN roles r ON r.id = rp.role_id
                    WHERE r.name = 'Admin' AND r.is_system = true
                    """
                )
            )
        ).scalar()

    await engine.dispose()

    assert total > 0, "No permissions found"
    assert admin_count == total, (
        f"Admin role has {admin_count} permissions but {total} exist"
    )


@pytest.mark.integration
async def test_pgvector_extension_installed(async_database_url):
    """The vector extension (pgvector) must be installed."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
        )
        found = result.scalar()

    await engine.dispose()
    assert found == 1, "pgvector extension is not installed"


# ── Phase 10 — messages + citations (migration 010) ───────────────────────────

@pytest.mark.integration
async def test_messages_and_citations_tables_exist(async_database_url, run_migrations):
    """Migration 010 must create messages + citations with the Phase 10
    CHECK constraints (role / groundedness / assistant-only metrics) and
    the RESTRICT FK policy on citations (roadmap Phase 10 step 3)."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))
            ).fetchall()
        }
        assert {"messages", "citations"} <= tables

        constraints = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ('messages'::regclass, 'citations'::regclass)"
                ))
            ).fetchall()
        }
        assert {
            "ck_messages_role",
            "ck_messages_groundedness",
            "ck_messages_assistant_only_metrics",
            "ck_citations_index",
        } <= constraints

        # FK policy (roadmap Phase 10 step 3): RESTRICT to document/version/
        # chunk/page — cited evidence cannot be silently deleted; message_id
        # is the one CASCADE (citations die with their message, DB §22).
        fks = {
            row[0]: row[1]
            for row in (
                await conn.execute(sa.text(
                    """
                    SELECT tc.constraint_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.table_constraints tc
                      ON tc.constraint_name = rc.constraint_name
                     AND tc.table_name = 'citations'
                    """
                ))
            ).fetchall()
        }
        for name, rule in fks.items():
            expected = "CASCADE" if name == "citations_message_id_fkey" else "RESTRICT"
            assert rule == expected, (
                f"{name} delete rule must be {expected}, got {rule}"
            )

    await engine.dispose()


@pytest.mark.integration
async def test_migration_010_downgrade_upgrade_cycle(async_database_url, run_migrations):
    """Phase 1 exit criterion, applied to the newest migration:
    downgrade -1 (drop 010) then upgrade head runs clean."""
    import os
    import subprocess
    import sys

    cwd = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sync_url = async_database_url.replace("+asyncpg", "+psycopg2")

    def _alembic(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env={**os.environ, "MIGRATION_DATABASE_URL": sync_url},
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    down = _alembic("downgrade", "-1")
    assert down.returncode == 0, (
        f"alembic downgrade -1 failed:\n{down.stdout}\n{down.stderr}"
    )
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, (
        f"alembic upgrade head failed:\n{up.stdout}\n{up.stderr}"
    )

    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))
            ).fetchall()
        }
    await engine.dispose()
    assert {"messages", "citations"} <= tables, (
        "messages/citations must exist again after the downgrade/upgrade cycle"
    )


# ── Phase 11 — conversations (migration 011) ──────────────────────────────────

@pytest.mark.integration
async def test_conversation_tables_and_constraints(async_database_url, run_migrations):
    """Migration 011 must create conversations / conversation_documents /
    message_feedback per DB §20–21 with the documented constraints, the
    messages→conversations FK (messages stay nullable for standalone /ask),
    and the DB §27 recency index."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))
            ).fetchall()
        }
        assert {"conversations", "conversation_documents", "message_feedback"} <= tables

        constraints = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ("
                    "'conversations'::regclass, "
                    "'conversation_documents'::regclass, "
                    "'message_feedback'::regclass)"
                ))
            ).fetchall()
        }
        assert {
            "ck_conversations_scope_type",
            "ck_message_feedback_rating",
            "uq_message_feedback_message_user",
            "pk_conversation_documents",
        } <= constraints

        # messages.conversation_id: FK CASCADE exists, column stays nullable
        fks = {
            row[0]: row[1]
            for row in (
                await conn.execute(sa.text(
                    """
                    SELECT tc.constraint_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.table_constraints tc
                      ON tc.constraint_name = rc.constraint_name
                     AND tc.table_name = 'messages'
                    """
                ))
            ).fetchall()
        }
        assert fks.get("fk_messages_conversation_id") == "CASCADE"

        nullable = (await conn.execute(sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = 'conversation_id'"
        ))).scalar_one()
        assert nullable == "YES", "standalone /ask answers keep conversation_id NULL"

        # DB §27: the conversation-list recency index
        indexes = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'conversations'"
                ))
            ).fetchall()
        }
        assert "ix_conversations_org_user_updated" in indexes

    await engine.dispose()


@pytest.mark.integration
async def test_migration_011_downgrade_upgrade_cycle(async_database_url, run_migrations):
    """downgrade -1 (drop 011) then upgrade head runs clean."""
    import os
    import subprocess
    import sys

    cwd = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sync_url = async_database_url.replace("+asyncpg", "+psycopg2")

    def _alembic(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env={**os.environ, "MIGRATION_DATABASE_URL": sync_url},
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    down = _alembic("downgrade", "-1")
    assert down.returncode == 0, (
        f"alembic downgrade -1 failed:\n{down.stdout}\n{down.stderr}"
    )
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, (
        f"alembic upgrade head failed:\n{up.stdout}\n{up.stderr}"
    )

    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))
            ).fetchall()
        }
    await engine.dispose()
    assert {"conversations", "conversation_documents", "message_feedback"} <= tables, (
        "conversation tables must exist again after the downgrade/upgrade cycle"
    )
