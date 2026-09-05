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


# ── Phase 12 — comparisons (migration 012, plan §10) ──────────────────────────

@pytest.mark.integration
async def test_comparison_tables_and_constraints(async_database_url, run_migrations):
    """Migration 012 must create document_comparisons / comparison_changes
    per plan §10 with the CHECK constraints, the unique pair constraint,
    the RESTRICT version FKs, the SET NULL chunk FKs, the CASCADE from
    processing_jobs, and the COMPARISON-pairing CHECK."""
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
        assert {"document_comparisons", "comparison_changes"} <= tables

        constraints = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ("
                    "'document_comparisons'::regclass, "
                    "'comparison_changes'::regclass, "
                    "'processing_jobs'::regclass)"
                ))
            ).fetchall()
        }
        assert {
            "ck_document_comparisons_status",
            "ck_document_comparisons_distinct_versions",
            "uq_document_comparisons_pair",
            "ck_comparison_changes_change_type",
            "ck_comparison_changes_severity",
            "ck_comparison_changes_added_no_old",
            "ck_comparison_changes_removed_no_new",
            "ck_processing_jobs_comparison_pairing",
        } <= constraints

    await engine.dispose()


@pytest.mark.integration
async def test_comparison_change_check_constraints_enforced(
    async_database_url, run_migrations
):
    """Functional CHECK verification (plan §10 acceptance): the
    comparison_changes CHECKs reject an invalid change_type / severity and an
    ADDED row carrying old_chunk_id (REMOVED symmetric); a minimal valid row
    inserts; a COMPARISON job without comparison_id violates the pairing
    CHECK.  All inside a transaction that is rolled back."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SAVEPOINT seed"))
        try:
            org_id = str((await conn.execute(sa.text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (gen_random_uuid(), 'mig12-org', 'mig12-org') RETURNING id"
            ))).scalar_one())
            user_id = str((await conn.execute(sa.text(
                "INSERT INTO users (id, organization_id, email, full_name, password_hash) "
                "VALUES (gen_random_uuid(), :org, 'mig12@x.test', 'Mig', 'x') RETURNING id"
            ), {"org": org_id})).scalar_one())
            doc_id = str((await conn.execute(sa.text(
                "INSERT INTO documents (id, organization_id, owner_id, name, document_type) "
                "VALUES (gen_random_uuid(), :org, :owner, 'Mig Doc', 'policy') RETURNING id"
            ), {"org": org_id, "owner": user_id})).scalar_one())

            async def _new_version(number: int) -> str:
                return str((await conn.execute(sa.text(
                    "INSERT INTO document_versions (id, document_id, version_number, "
                    "storage_key, mime_type, file_size_bytes, status, created_by) "
                    "VALUES (gen_random_uuid(), :doc, :n, 'k', 'application/pdf', 1, "
                    "'READY', :owner) RETURNING id"
                ), {"doc": doc_id, "n": number, "owner": user_id})).scalar_one())

            ver_a, ver_b = await _new_version(1), await _new_version(2)
            page_id = str((await conn.execute(sa.text(
                "INSERT INTO document_pages (id, document_version_id, page_number, text) "
                "VALUES (gen_random_uuid(), :ver, 1, 'text') RETURNING id"
            ), {"ver": ver_a})).scalar_one())
            chunk_id = str((await conn.execute(sa.text(
                "INSERT INTO document_chunks (id, organization_id, document_version_id, "
                "page_id, chunk_index, content, content_hash, token_count) "
                "VALUES (gen_random_uuid(), :org, :ver, :page, 0, 'c', :h, 1) RETURNING id"
            ), {"org": org_id, "ver": ver_a, "page": page_id,
                "h": "0" * 64})).scalar_one())
            cmp_id = str((await conn.execute(sa.text(
                "INSERT INTO document_comparisons (id, organization_id, "
                "document_a_version_id, document_b_version_id, status, requested_by) "
                "VALUES (gen_random_uuid(), :org, :va, :vb, 'PENDING', :owner) RETURNING id"
            ), {"org": org_id, "va": ver_a, "vb": ver_b,
                "owner": user_id})).scalar_one())

            async def _must_raise(stmt: str, label: str) -> None:
                await conn.execute(sa.text("SAVEPOINT bad"))
                raised = False
                try:
                    await conn.execute(sa.text(stmt))
                except Exception:
                    raised = True
                finally:
                    await conn.execute(sa.text("ROLLBACK TO SAVEPOINT bad"))
                assert raised, f"{label} must be rejected"

            base = (
                "INSERT INTO comparison_changes (comparison_id, change_type, severity"
            )
            await _must_raise(
                base + f") VALUES ('{cmp_id}', 'BOGUS', 'MAJOR')",
                "invalid change_type",
            )
            await _must_raise(
                base + f") VALUES ('{cmp_id}', 'MODIFIED', 'HUGE')",
                "invalid severity",
            )
            await _must_raise(
                base + f", old_chunk_id) VALUES ('{cmp_id}', 'ADDED', 'MINOR', "
                f"'{chunk_id}')",
                "ADDED carrying old_chunk_id",
            )
            await _must_raise(
                base + f", new_chunk_id) VALUES ('{cmp_id}', 'REMOVED', 'MINOR', "
                f"'{chunk_id}')",
                "REMOVED carrying new_chunk_id",
            )

            # Control: a minimal valid row inserts
            await conn.execute(sa.text(
                base + f") VALUES ('{cmp_id}', 'MODIFIED', 'MINOR')"
            ))

            # Pairing CHECK: a non-COMPARISON job with comparison_id → rejected;
            # a COMPARISON job with comparison_id → accepted
            await _must_raise(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type, comparison_id) "
                f"VALUES (gen_random_uuid(), '{org_id}', '{ver_a}', 'EXTRACTION', "
                f"'{cmp_id}')",
                "non-COMPARISON job with comparison_id",
            )
            await conn.execute(sa.text(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type, comparison_id) "
                f"VALUES (gen_random_uuid(), '{org_id}', '{ver_a}', 'COMPARISON', "
                f"'{cmp_id}')"
            ))
        finally:
            await conn.execute(sa.text("ROLLBACK TO SAVEPOINT seed"))
    await engine.dispose()


# ── Phase 13 — conflicts (migration 013, plan §9) ─────────────────────────────

@pytest.mark.integration
async def test_conflict_tables_and_constraints(async_database_url, run_migrations):
    """Migration 013 must create conflicts / conflict_statements per plan §9
    with the CHECK constraints, the resolution-fields invariant, the unique
    (conflict_id, chunk_id) statement constraint, the nullable
    document_version_id + checkpoint column + version-required CHECK on
    processing_jobs, and the RESTRICT statement FK policy."""
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
        assert {"conflicts", "conflict_statements"} <= tables

        constraints = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ("
                    "'conflicts'::regclass, "
                    "'conflict_statements'::regclass, "
                    "'processing_jobs'::regclass)"
                ))
            ).fetchall()
        }
        assert {
            "ck_conflicts_severity",
            "ck_conflicts_status",
            "ck_conflicts_detection_method",
            "ck_conflicts_resolution_fields",
            "uq_conflict_statements_conflict_chunk",
            "ck_processing_jobs_version_required",
        } <= constraints

        # FK policy (plan §9.2): the statement's provenance chains are
        # RESTRICT (citation-grade — evidence cannot be silently deleted),
        # while conflict_id is CASCADE (statements die with their conflict).
        fks = {
            row[0]: row[1]
            for row in (
                await conn.execute(sa.text(
                    """
                    SELECT tc.constraint_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.table_constraints tc
                      ON tc.constraint_name = rc.constraint_name
                     AND tc.table_name = 'conflict_statements'
                    """
                ))
            ).fetchall()
        }
        for name, rule in fks.items():
            expected = (
                "CASCADE"
                if name == "conflict_statements_conflict_id_fkey"
                else "RESTRICT"
            )
            assert rule == expected, (
                f"{name} delete rule must be {expected}, got {rule}"
            )

        # processing_jobs.document_version_id is now nullable and checkpoint exists
        columns = {
            row[0]: (row[1], row[2])
            for row in (
                await conn.execute(sa.text(
                    "SELECT column_name, is_nullable, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'processing_jobs'"
                ))
            ).fetchall()
        }
        assert columns["document_version_id"][0] == "YES"
        assert columns["checkpoint"][1] == "jsonb"

    await engine.dispose()


@pytest.mark.integration
async def test_conflict_check_constraints_enforced(async_database_url, run_migrations):
    """Functional CHECK verification (plan §23 Task 2 acceptance):
    invalid status/severity/detection_method values are rejected; the
    resolution-fields invariant rejects an OPEN row with resolver data and a
    REVIEWED row without it; the version-required CHECK rejects a
    non-CONFLICT_SCAN job with NULL document_version_id while allowing a
    CONFLICT_SCAN one; conflict:resolve is seeded to Admin/Editor, never
    Viewer.  All inside a transaction that is rolled back."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SAVEPOINT seed"))
        try:
            org_id = str((await conn.execute(sa.text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (gen_random_uuid(), 'mig13-org', 'mig13-org') RETURNING id"
            ))).scalar_one())

            async def _must_raise(stmt: str, label: str) -> None:
                await conn.execute(sa.text("SAVEPOINT bad"))
                raised = False
                try:
                    await conn.execute(sa.text(stmt))
                except Exception:
                    raised = True
                finally:
                    await conn.execute(sa.text("ROLLBACK TO SAVEPOINT bad"))
                assert raised, f"{label} must be rejected"

            base = "INSERT INTO conflicts (organization_id, topic, severity, status, detection_method"
            await _must_raise(
                base + f") VALUES ('{org_id}', 't', 'HUGE', 'OPEN', 'BACKGROUND_SCAN')",
                "invalid severity",
            )
            await _must_raise(
                base + f") VALUES ('{org_id}', 't', 'MAJOR', 'FROZEN', 'BACKGROUND_SCAN')",
                "invalid status",
            )
            await _must_raise(
                base + f") VALUES ('{org_id}', 't', 'MAJOR', 'OPEN', 'PSYCHIC')",
                "invalid detection_method",
            )
            await _must_raise(
                base + ", resolved_by, resolved_at) VALUES ("
                f"'{org_id}', 't', 'MAJOR', 'OPEN', 'BACKGROUND_SCAN', "
                "gen_random_uuid(), now())",
                "OPEN row carrying resolver data",
            )
            await _must_raise(
                base + f") VALUES ('{org_id}', 't', 'MAJOR', 'REVIEWED', 'BACKGROUND_SCAN')",
                "REVIEWED row without resolver data",
            )

            # Control: a minimal valid OPEN conflict inserts
            conflict_id = str((await conn.execute(sa.text(
                "INSERT INTO conflicts (organization_id, topic, severity, status, "
                "detection_method) VALUES (:org, 't', 'MAJOR', 'OPEN', "
                "'BACKGROUND_SCAN') RETURNING id"
            ), {"org": org_id})).scalar_one())

            # Version-required CHECK
            await _must_raise(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type) "
                f"VALUES (gen_random_uuid(), '{org_id}', NULL, 'EXTRACTION')",
                "non-CONFLICT_SCAN job with NULL document_version_id",
            )
            await conn.execute(sa.text(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type, checkpoint) "
                "VALUES (gen_random_uuid(), :org, NULL, 'CONFLICT_SCAN', NULL)"
            ), {"org": org_id})

            # Permission seeding: conflict:resolve granted to Admin + Editor,
            # never Viewer
            grants = {
                row[0]
                for row in (
                    await conn.execute(sa.text(
                        "SELECT r.name FROM role_permissions rp "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "JOIN roles r ON r.id = rp.role_id "
                        "WHERE p.key = 'conflict:resolve' AND r.is_system = true"
                    ))
                ).fetchall()
            }
            assert grants == {"Admin", "Editor"}, (
                f"conflict:resolve must be granted to Admin+Editor only, got {grants}"
            )
        finally:
            await conn.execute(sa.text("ROLLBACK TO SAVEPOINT seed"))
    await engine.dispose()


@pytest.mark.integration
async def test_migration_013_downgrade_upgrade_cycle(async_database_url, run_migrations):
    """downgrade -1 (drop 013) then upgrade head runs clean (plan §34 item 2)."""
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
        f"alembic upgrade head failed:\n{up.stdout}\n{down.stderr}"
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
    assert {"conflicts", "conflict_statements"} <= tables, (
        "conflict tables must exist again after the downgrade/upgrade cycle"
    )


# ── Phase 14 — summaries + extractions (migration 014, plan §4) ───────────────

@pytest.mark.integration
async def test_summary_extraction_tables_and_constraints(
    async_database_url, run_migrations
):
    """Migration 014 must create document_summaries / document_extractions /
    document_extraction_items per plan §4 with the status/schema/category
    CHECK constraints, the one-row-per-version summary UNIQUE, the item
    run/category/index UNIQUE, the RESTRICT provenance FK policy on items,
    the CASCADE run FK on items, and the new pairing CHECKs + widened
    job_type CHECK on processing_jobs."""
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
        assert {
            "document_summaries",
            "document_extractions",
            "document_extraction_items",
        } <= tables

        constraints = {
            row[0]
            for row in (
                await conn.execute(sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ("
                    "'document_summaries'::regclass, "
                    "'document_extractions'::regclass, "
                    "'document_extraction_items'::regclass, "
                    "'processing_jobs'::regclass)"
                ))
            ).fetchall()
        }
        assert {
            "ck_document_summaries_status",
            "uq_document_summaries_version",
            "ck_document_extractions_status",
            "ck_document_extractions_schema_key",
            "ck_document_extraction_items_category",
            "ck_document_extraction_items_page_number",
            "uq_document_extraction_items_run_category_index",
            "ck_processing_jobs_summary_pairing",
            "ck_processing_jobs_extraction_pairing",
        } <= constraints

        # The widened job_type CHECK must accept STRUCTURED_EXTRACTION while
        # keeping the pre-existing values (plan §2.4's distinct-name rule).
        definition = (await conn.execute(sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_processing_jobs_job_type'"
        ))).scalar_one()
        assert "STRUCTURED_EXTRACTION" in definition
        assert "SUMMARY" in definition
        assert "CONFLICT_SCAN" in definition

        # Item FK policy: extraction_id CASCADE (items die with their run);
        # every provenance chain RESTRICT (citation-grade, plan §4.4).
        fks = {
            row[0]: row[1]
            for row in (
                await conn.execute(sa.text(
                    """
                    SELECT tc.constraint_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.table_constraints tc
                      ON tc.constraint_name = rc.constraint_name
                     AND tc.table_name = 'document_extraction_items'
                    """
                ))
            ).fetchall()
        }
        for name, rule in fks.items():
            expected = (
                "CASCADE"
                if name == "document_extraction_items_extraction_id_fkey"
                else "RESTRICT"
            )
            assert rule == expected, (
                f"{name} delete rule must be {expected}, got {rule}"
            )

    await engine.dispose()


@pytest.mark.integration
async def test_phase14_permissions_seeded(async_database_url, run_migrations):
    """summary:regenerate + extraction:create seeded to Admin/Editor, never
    Viewer (mirrors the conflict:resolve assertions)."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.connect() as conn:
        for key in ("summary:regenerate", "extraction:create"):
            grants = {
                row[0]
                for row in (
                    await conn.execute(sa.text(
                        "SELECT r.name FROM role_permissions rp "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "JOIN roles r ON r.id = rp.role_id "
                        "WHERE p.key = :key AND r.is_system = true"
                    ), {"key": key})
                ).fetchall()
            }
            assert grants == {"Admin", "Editor"}, (
                f"{key} must be granted to Admin+Editor only, got {grants}"
            )
    await engine.dispose()


@pytest.mark.integration
async def test_phase14_check_constraints_enforced(async_database_url, run_migrations):
    """Functional CHECK verification for migration 014: invalid status /
    schema_key / category values are rejected; two summaries for one version
    violate the UNIQUE; a SUMMARY job without summary_id violates the pairing
    CHECK.  All inside a transaction that is rolled back."""
    engine = create_async_engine(async_database_url, echo=False)
    async with engine.begin() as conn:
        await conn.execute(sa.text("SAVEPOINT seed"))
        try:
            org_id = str((await conn.execute(sa.text(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (gen_random_uuid(), 'mig14-org', 'mig14-org') RETURNING id"
            ))).scalar_one())
            user_id = str((await conn.execute(sa.text(
                "INSERT INTO users (id, organization_id, email, full_name, password_hash) "
                "VALUES (gen_random_uuid(), :org, 'mig14@x.test', 'Mig', 'x') RETURNING id"
            ), {"org": org_id})).scalar_one())
            doc_id = str((await conn.execute(sa.text(
                "INSERT INTO documents (id, organization_id, owner_id, name, document_type) "
                "VALUES (gen_random_uuid(), :org, :owner, 'Mig Doc 14', 'policy') RETURNING id"
            ), {"org": org_id, "owner": user_id})).scalar_one())
            ver_id = str((await conn.execute(sa.text(
                "INSERT INTO document_versions (id, document_id, version_number, "
                "storage_key, mime_type, file_size_bytes, status, created_by) "
                "VALUES (gen_random_uuid(), :doc, 1, 'k', 'application/pdf', 1, "
                "'READY', :owner) RETURNING id"
            ), {"doc": doc_id, "owner": user_id})).scalar_one())

            async def _must_raise(stmt: str, params: dict, label: str) -> None:
                await conn.execute(sa.text("SAVEPOINT bad"))
                raised = False
                try:
                    await conn.execute(sa.text(stmt), params)
                except Exception:
                    raised = True
                finally:
                    await conn.execute(sa.text("ROLLBACK TO SAVEPOINT bad"))
                assert raised, f"{label} must be rejected"

            await _must_raise(
                "INSERT INTO document_summaries (organization_id, document_id, "
                "document_version_id, status, requested_by) VALUES (:org, :doc, "
                ":ver, 'FROZEN', :owner)",
                {"org": org_id, "doc": doc_id, "ver": ver_id, "owner": user_id},
                "invalid summary status",
            )
            await _must_raise(
                "INSERT INTO document_extractions (organization_id, document_id, "
                "document_version_id, schema_key, status, requested_by) VALUES "
                "(:org, :doc, :ver, 'custom_x', 'PENDING', :owner)",
                {"org": org_id, "doc": doc_id, "ver": ver_id, "owner": user_id},
                "invalid schema_key",
            )
            await _must_raise(
                "INSERT INTO document_summaries (organization_id, document_id, "
                "document_version_id, status, requested_by) VALUES (:org, :doc, "
                ":ver, 'PENDING', :owner)",
                {"org": org_id, "doc": doc_id, "ver": ver_id, "owner": user_id},
                "second summary for the same version (UNIQUE)",
            )

            run_id = str((await conn.execute(sa.text(
                "INSERT INTO document_extractions (organization_id, document_id, "
                "document_version_id, schema_key, status, requested_by) "
                "VALUES (:org, :doc, :ver, 'standard_v1', 'PENDING', :owner) "
                "RETURNING id"
            ), {"org": org_id, "doc": doc_id, "ver": ver_id,
                "owner": user_id})).scalar_one())

            await _must_raise(
                "INSERT INTO document_extraction_items (extraction_id, category, "
                "item_index, label, document_id, document_version_id, chunk_id, "
                "page_id, page_number, quoted_text) "
                "SELECT :run, 'vehicle', 0, 'l', d.id, dv.id, "
                "gen_random_uuid(), gen_random_uuid(), 0, 'q' "
                "FROM documents d, document_versions dv "
                "WHERE d.id = :doc AND dv.id = :ver",
                {"run": run_id, "doc": doc_id, "ver": ver_id},
                "invalid item category",
            )

            # Pairing CHECKs
            await _must_raise(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type) VALUES "
                "(gen_random_uuid(), :org, :ver, 'SUMMARY')",
                {"org": org_id, "ver": ver_id},
                "SUMMARY job without summary_id",
            )
            await _must_raise(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type, extraction_id) VALUES "
                "(gen_random_uuid(), :org, :ver, 'COMPARISON', :run)",
                {"org": org_id, "ver": ver_id, "run": run_id},
                "non-STRUCTURED_EXTRACTION job with extraction_id",
            )
            await conn.execute(sa.text(
                "INSERT INTO processing_jobs (id, organization_id, "
                "document_version_id, job_type, extraction_id) VALUES "
                "(gen_random_uuid(), :org, :ver, 'STRUCTURED_EXTRACTION', :run)"
            ), {"org": org_id, "ver": ver_id, "run": run_id})
        finally:
            await conn.execute(sa.text("ROLLBACK TO SAVEPOINT seed"))
    await engine.dispose()


@pytest.mark.integration
async def test_migration_014_downgrade_upgrade_cycle(async_database_url, run_migrations):
    """downgrade -1 (drop 014) then upgrade head runs clean (plan §14)."""
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
        f"alembic upgrade head failed:\n{up.stdout}\n{down.stderr}"
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
    assert {
        "document_summaries",
        "document_extractions",
        "document_extraction_items",
    } <= tables, (
        "Phase 14 tables must exist again after the downgrade/upgrade cycle"
    )
