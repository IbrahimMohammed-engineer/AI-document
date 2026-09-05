"""
API tests — /summaries (Phase 14, plan §8.4).

Covers: the create-then-poll flow (202 on creation → 200 when reusing);
POST /regenerate permission gate (403 for a Viewer-role user);
regenerate-when-absent behaves like create; and the 404-not-403
non-leaking matrix (unknown document id).
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.llm import set_llm_provider
from tests.fixtures.summary_fixtures import (
    install_summary_llm_stub,
    seed_summary_document,
)

from tests.api.test_compare import _org_id, _register_and_login, _set_user_role, _uuid  # noqa: F401


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture()
async def summaries_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
):
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    install_summary_llm_stub()
    yield app_session_factory
    set_llm_provider(None)
    await pool.aclose()


async def _seed_doc(factory: async_sessionmaker, org_id: str, user_id: str) -> dict:
    return await seed_summary_document(
        factory, organization_id=org_id, owner_id=user_id
    )


@pytest.mark.integration
class TestSummariesAPI:

    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.get(f"/summaries/{uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_get_creates_then_reuses(
        self, app_client, app_session_factory, summaries_env
    ):
        """Create-then-poll flow: 202 when the row+job are created, then
        200 with the same row id (nothing recomputed)."""
        token, _slug = await _register_and_login(app_client)
        user_id_resp = await app_client.get(
            "/auth/me", headers=_auth_headers(token)
        )
        user_id = user_id_resp.json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await _seed_doc(app_session_factory, org_id, user_id)

        first = await app_client.get(
            f"/summaries/{doc['document_id']}", headers=_auth_headers(token)
        )
        assert first.status_code == 202, first.text
        assert first.json()["status"] in ("PENDING", "PROCESSING")
        summary_id = first.json()["id"]

        second = await app_client.get(
            f"/summaries/{doc['document_id']}", headers=_auth_headers(token)
        )
        assert second.status_code == 200
        assert second.json()["id"] == summary_id

    async def test_unknown_document_returns_404(self, app_client, summaries_env):
        token, _ = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/summaries/{_uuid()}", headers=_auth_headers(token)
        )
        assert resp.status_code == 404

    async def test_regenerate_returns_202_and_resets_pending(
        self, app_client, app_session_factory, summaries_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await _seed_doc(app_session_factory, org_id, user_id)

        # Create first so regenerate transitions the EXISTING row.
        created = await app_client.get(
            f"/summaries/{doc['document_id']}", headers=_auth_headers(token)
        )
        assert created.status_code == 202

        regen = await app_client.post(
            f"/summaries/{doc['document_id']}/regenerate",
            headers=_auth_headers(token),
            json={"version": None},
        )
        assert regen.status_code == 202, regen.text
        assert regen.json()["status"] == "PENDING"
        assert regen.json()["id"] == created.json()["id"]

    async def test_regenerate_when_absent_behaves_like_create(
        self, app_client, app_session_factory, summaries_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await _seed_doc(app_session_factory, org_id, user_id)

        regen = await app_client.post(
            f"/summaries/{doc['document_id']}/regenerate",
            headers=_auth_headers(token),
            json={"version": None},
        )
        assert regen.status_code == 202, regen.text
        assert regen.json()["status"] == "PENDING"

    async def test_regenerate_requires_permission(
        self, app_client, app_session_factory, summaries_env
    ):
        """A Viewer-role user gets 403 (permission-gated beyond read)."""
        token, slug = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await _seed_doc(app_session_factory, org_id, user_id)

        await _set_user_role(app_session_factory, user_id, org_id, "Viewer")
        resp = await app_client.post(
            f"/summaries/{doc['document_id']}/regenerate",
            headers=_auth_headers(token),
            json={"version": None},
        )
        assert resp.status_code == 403

    async def test_specific_version_resolution(
        self, app_client, app_session_factory, summaries_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await _seed_doc(app_session_factory, org_id, user_id)

        resp = await app_client.get(
            f"/summaries/{doc['document_id']}?version=1",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 202
        assert resp.json()["document_version_id"] == doc["version_id"]
