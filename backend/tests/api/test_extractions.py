"""
API tests — /extractions (Phase 14, plan §8.4).

Covers: POST permission gate (403 for Viewer, 202 for Admin); run lifecycle
(PENDING until the worker runs); GET detail; the run-history list endpoint's
pagination/ordering (most recent first); 404-not-403 for unknown runs.
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
from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.llm import set_llm_provider
from tests.fixtures.extraction_fixtures import (
    install_extraction_llm_stub,
    seed_contract_document,
)

from tests.api.test_compare import _org_id, _register_and_login, _set_user_role, _uuid  # noqa: F401


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture()
async def extractions_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
):
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    install_extraction_llm_stub()
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    yield app_session_factory
    set_llm_provider(None)
    set_embedding_provider(None)
    await pool.aclose()


@pytest.mark.integration
class TestExtractionsAPI:

    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.post("/extractions", json={
            "document_id": str(uuid.uuid4()),
        })
        assert resp.status_code == 401

    async def test_create_run_returns_202(
        self, app_client, app_session_factory, extractions_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )

        resp = await app_client.post(
            "/extractions",
            headers=_auth_headers(token),
            json={"document_id": doc["document_id"], "schema_key": "standard_v1"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] in ("PENDING", "PROCESSING")
        assert body["schema_key"] == "standard_v1"

        # Every explicit call is a NEW run (audit trail, no reuse).
        resp2 = await app_client.post(
            "/extractions",
            headers=_auth_headers(token),
            json={"document_id": doc["document_id"]},
        )
        assert resp2.status_code == 202
        assert resp2.json()["id"] != body["id"]

    async def test_create_run_requires_permission(
        self, app_client, app_session_factory, extractions_env
    ):
        token, slug = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )

        await _set_user_role(app_session_factory, user_id, org_id, "Viewer")
        resp = await app_client.post(
            "/extractions",
            headers=_auth_headers(token),
            json={"document_id": doc["document_id"]},
        )
        assert resp.status_code == 403

    async def test_get_run_detail(
        self, app_client, app_session_factory, extractions_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )

        created = await app_client.post(
            "/extractions",
            headers=_auth_headers(token),
            json={"document_id": doc["document_id"]},
        )
        run_id = created.json()["id"]

        detail = await app_client.get(
            f"/extractions/{run_id}", headers=_auth_headers(token)
        )
        assert detail.status_code == 200
        assert detail.json()["status"] in ("PENDING", "PROCESSING")
        # Items only present (non-null) when COMPLETED.
        assert detail.json()["items"] is None

    async def test_unknown_run_returns_404(self, app_client, extractions_env):
        token, _ = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/extractions/{_uuid()}", headers=_auth_headers(token)
        )
        assert resp.status_code == 404

    async def test_run_history_most_recent_first(
        self, app_client, app_session_factory, extractions_env
    ):
        token, _ = await _register_and_login(app_client)
        user_id = (
            await app_client.get("/auth/me", headers=_auth_headers(token))
        ).json()["id"]
        org_id = await _org_id(app_session_factory, user_id)
        doc = await seed_contract_document(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )

        run_ids = []
        for _ in range(3):
            created = await app_client.post(
                "/extractions",
                headers=_auth_headers(token),
                json={"document_id": doc["document_id"]},
            )
            assert created.status_code == 202
            run_ids.append(created.json()["id"])

        listing = await app_client.get(
            f"/documents/{doc['document_id']}/extractions?limit=2&offset=0",
            headers=_auth_headers(token),
        )
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        # Most recent first (the newest created run leads the page).
        assert body["items"][0]["id"] == run_ids[-1]

        page2 = await app_client.get(
            f"/documents/{doc['document_id']}/extractions?limit=2&offset=2",
            headers=_auth_headers(token),
        )
        assert page2.status_code == 200
        assert len(page2.json()["items"]) == 1

    async def test_history_unknown_document_returns_404(
        self, app_client, extractions_env
    ):
        token, _ = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/documents/{_uuid()}/extractions", headers=_auth_headers(token)
        )
        assert resp.status_code == 404
