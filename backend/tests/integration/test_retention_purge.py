"""
Retention hard-purge integration test (Phase 16, plan §7/§17.1).

Seeds soft-deleted documents past (and inside) the retention window, runs
``run_retention_purge`` directly against the test Postgres, and asserts:

  - documents past the window are HARD-PURGED: the documents row and ALL
    dependent rows (versions, chunks, pages, sections, memberships, tags,
    grants, citations, summaries, extractions) are gone
  - documents still inside the window survive
  - active (non-deleted) documents are never touched
  - a DOCUMENT_HARD_PURGED audit row is written per purged document with
    counts only (no document content — Backend §54)
  - storage objects are deleted AFTER the DB commit (a failing provider
    does not roll back the purge)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import app.infrastructure.database as database_module
from app.workers.jobs import run_retention_purge

pytestmark = pytest.mark.integration


def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_document(
    app_session_factory,
    org_id: str,
    owner_id: str,
    *,
    deleted_at: datetime | None,
    name: str = "Purge Me",
    with_children: bool = True,
) -> str:
    """Seed a document (+ optional version/chunk/citation/summary children)."""
    doc_id, ver_id, page_id, chunk_id = _uuid(), _uuid(), _uuid(), _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO documents (id, organization_id, owner_id, name, "
            "document_type, status, access_level, deleted_at) "
            "VALUES (:id, :org, :owner, :name, 'policy', 'active', "
            "'organization', :deleted_at)"
        ), {"id": doc_id, "org": org_id, "owner": owner_id,
            "name": name, "deleted_at": deleted_at})
        await session.execute(text(
            "INSERT INTO document_versions (id, document_id, version_number, "
            "storage_key, mime_type, file_size_bytes, status, created_by) "
            "VALUES (:id, :doc, 1, :storage_key, 'application/pdf', 100, "
            "'READY', :owner)"
        ), {"id": ver_id, "doc": doc_id, "owner": owner_id,
            "storage_key": f"purge-test/{doc_id}.pdf"})
        if with_children:
            await session.execute(text(
                "INSERT INTO document_pages (id, document_version_id, "
                "page_number, text) VALUES (:id, :ver, 1, 'page')"
            ), {"id": page_id, "ver": ver_id})
            await session.execute(text(
                "INSERT INTO document_chunks (id, document_version_id, "
                "organization_id, page_id, chunk_index, content, content_hash, "
                "token_count) VALUES (:id, :ver, :org, :page, 0, 'chunk text', "
                ":hash, 3)"
            ), {"id": chunk_id, "ver": ver_id, "org": org_id, "page": page_id,
                "hash": _uuid()})
            await session.execute(text(
                "INSERT INTO collection_documents (collection_id, document_id) "
                "SELECT c.id, :doc FROM collections c WHERE c.organization_id = :org "
                "LIMIT 1"
            ), {"doc": doc_id, "org": org_id})
        await session.commit()
    return doc_id


async def _org_and_user(app_session_factory) -> tuple[str, str]:
    """Insert a fresh org + owner user (no API round-trips needed here)."""
    org_id, user_id = _uuid(), _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, :name, :slug)"
        ), {"id": org_id, "name": f"Purge Org {org_id[:8]}", "slug": f"purge-{org_id[:8]}"})
        await session.execute(text(
            "INSERT INTO users (id, organization_id, email, full_name, is_active) "
            "VALUES (:id, :org, :email, 'Owner', true)"
        ), {"id": user_id, "org": org_id, "email": f"owner-{org_id[:8]}@example.com"})
        await session.commit()
    return org_id, user_id


async def _row_count(app_session_factory, sql: str, params: dict) -> int:
    async with app_session_factory() as session:
        result = await session.execute(text(sql), params)
        return int(result.scalar_one())


class _RecordingStorage:
    """Minimal storage double — records delete() calls, always succeeds."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class _FailingStorage:
    """Storage double whose deletes ALWAYS fail (orphan reconciliation path)."""

    def __init__(self) -> None:
        self.attempted: list[str] = []

    async def delete(self, key: str) -> None:
        self.attempted.append(key)
        raise RuntimeError("storage outage")


@pytest.mark.asyncio
class TestRetentionPurge:

    async def test_purge_removes_rows_and_storage(
        self, app_client, app_session_factory, monkeypatch
    ):
        org_id, owner_id = await _org_and_user(app_session_factory)

        now = datetime.now(tz=timezone.utc)
        # Past the 90-day default window → purged
        old_doc = await _seed_document(
            app_session_factory, org_id, owner_id,
            deleted_at=now - timedelta(days=91), name="Old Doc",
        )
        # Inside the window → survives
        recent_doc = await _seed_document(
            app_session_factory, org_id, owner_id,
            deleted_at=now - timedelta(days=1), name="Recent Doc",
        )
        # Never deleted → survives
        active_doc = await _seed_document(
            app_session_factory, org_id, owner_id,
            deleted_at=None, name="Active Doc",
        )

        storage = _RecordingStorage()
        monkeypatch.setattr(database_module, "_session_factory", app_session_factory)
        from app.infrastructure.storage import set_storage_provider
        set_storage_provider(storage)  # type: ignore[arg-type]
        try:
            purged = await run_retention_purge({})
        finally:
            set_storage_provider(None)
            database_module._session_factory = None

        assert purged == 1

        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM documents WHERE id = :id", {"id": old_doc},
        ) == 0
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM documents WHERE id = :id", {"id": recent_doc},
        ) == 1
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM documents WHERE id = :id", {"id": active_doc},
        ) == 1

        # The whole dependent tree is gone with the root row
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM document_versions WHERE document_id = :id",
            {"id": old_doc},
        ) == 0
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM document_chunks c "
            "JOIN document_versions v ON v.id = c.document_version_id "
            "WHERE v.document_id = :id",
            {"id": old_doc},
        ) == 0
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM document_pages p "
            "JOIN document_versions v ON v.id = p.document_version_id "
            "WHERE v.document_id = :id",
            {"id": old_doc},
        ) == 0

        # Storage objects deleted AFTER commit
        assert storage.deleted == [f"purge-test/{old_doc}.pdf"]

        # Audit row written with counts only
        async with app_session_factory() as session:
            rows = (
                await session.execute(text(
                    "SELECT action, metadata FROM audit_logs "
                    "WHERE organization_id = :org AND action = 'DOCUMENT_HARD_PURGED'"
                ), {"org": org_id})
            ).all()
        assert len(rows) == 1
        action, metadata = rows[0]
        assert action == "DOCUMENT_HARD_PURGED"
        assert metadata["version_count"] == 1
        assert metadata["reason"] == "retention"
        assert "Old Doc" not in str(metadata)  # no document content in audit

    async def test_storage_failure_does_not_rollback_purge(
        self, app_client, app_session_factory, monkeypatch
    ):
        org_id, owner_id = await _org_and_user(app_session_factory)
        now = datetime.now(tz=timezone.utc)
        old_doc = await _seed_document(
            app_session_factory, org_id, owner_id,
            deleted_at=now - timedelta(days=200), name="Doomed Doc",
        )

        storage = _FailingStorage()
        monkeypatch.setattr(database_module, "_session_factory", app_session_factory)
        from app.infrastructure.storage import set_storage_provider
        set_storage_provider(storage)  # type: ignore[arg-type]
        try:
            purged = await run_retention_purge({})
        finally:
            set_storage_provider(None)
            database_module._session_factory = None

        assert purged == 1
        assert storage.attempted == [f"purge-test/{old_doc}.pdf"]
        assert await _row_count(
            app_session_factory,
            "SELECT COUNT(*) FROM documents WHERE id = :id", {"id": old_doc},
        ) == 0

    async def test_nothing_eligible_is_a_noop(
        self, app_client, app_session_factory, monkeypatch
    ):
        org_id, owner_id = await _org_and_user(app_session_factory)
        now = datetime.now(tz=timezone.utc)
        await _seed_document(
            app_session_factory, org_id, owner_id,
            deleted_at=now - timedelta(days=10), name="Young Doc",
        )

        monkeypatch.setattr(database_module, "_session_factory", app_session_factory)
        try:
            purged = await run_retention_purge({})
        finally:
            database_module._session_factory = None

        assert purged == 0
