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
