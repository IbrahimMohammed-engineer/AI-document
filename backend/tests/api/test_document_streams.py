"""
API tests — document processing SSE streams (Phase 11, FE §11.2).

Requires testcontainers (Postgres + Redis).  The stream's correctness does
NOT depend on pub/sub delivery: every relay wake (or synthetic poll tick)
re-reads the authoritative processing_jobs state from PostgreSQL — these
tests drive real DB transitions and assert the events follow.

Tests (roadmap Phase 11 §Testing — processing SSE row):
  - READY document: a single initial `status` event, stream closes.
  - Multiplexed /documents/stream?ids=: one connection, per-document ids.
  - Live transition: PROCESSING → terminal emits a changed `status` event,
    then the connection closes (the poll fallback path — the same re-read
    the relay wake triggers).
  - Unknown / foreign-org documents: 404 BEFORE the stream opens.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider


@pytest.fixture(autouse=True, scope="module")
def _stub_providers():
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    yield
    set_embedding_provider(None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _uuid() -> str:
    return str(uuid.uuid4())


async def _register_and_login(client) -> str:
    slug = f"stream-test-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Stream Test Org {slug}",
        "slug": slug,
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "full_name": "Stream Tester",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _current_user_id(client, token: str) -> str:
    resp = await client.get("/auth/me", headers=_auth_headers(token))
    return resp.json()["id"]


async def _get_org_id(app_session_factory, user_id: str) -> str:
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"),
            {"id": user_id},
        )
        return str(row.scalar_one())


async def _seed_document_with_version(
    app_session_factory,
    org_id: str,
    owner_id: str,
    *,
    version_status: str = "READY",
) -> str:
    doc_id, ver_id = _uuid(), _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO documents (id, organization_id, owner_id, name, document_type, "
            "status, access_level) VALUES (:id, :org, :owner, :name, 'policy', "
            "'active', 'organization')"
        ), {"id": doc_id, "org": org_id, "owner": owner_id,
            "name": f"Doc {doc_id[:6]}"})
        await session.execute(text(
            "INSERT INTO document_versions (id, document_id, version_number, storage_key, "
            "mime_type, file_size_bytes, status, created_by) "
            "VALUES (:id, :doc, 1, 'key', 'application/pdf', 100, :status, :owner)"
        ), {"id": ver_id, "doc": doc_id, "owner": owner_id, "status": version_status})
        await session.commit()
    return doc_id


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event_name = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if event_name:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event_name, data))
    return events


# ── Terminal / contract behaviour ─────────────────────────────────────────────

@pytest.mark.integration
class TestStreamContract:

    async def test_ready_document_single_event_then_close(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        doc_id = await _seed_document_with_version(
            app_session_factory, org_id, user_id, version_status="READY"
        )

        async with app_client.stream(
            "GET", f"/documents/{doc_id}/stream", headers=_auth_headers(token)
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = ""
            async for chunk in response.aiter_text():
                raw += chunk
        events = _parse_sse(raw)
        assert len(events) == 1
        name, data = events[0]
        assert name == "status"
        assert data["status"] == "READY"
        assert data["progress"] == 100
        assert "document_id" not in data  # single-document stream omits the id

    async def test_multiplexed_stream_includes_document_ids(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        doc_a = await _seed_document_with_version(
            app_session_factory, org_id, user_id, version_status="READY"
        )
        doc_b = await _seed_document_with_version(
            app_session_factory, org_id, user_id, version_status="READY"
        )

        async with app_client.stream(
            "GET", f"/documents/stream?ids={doc_a},{doc_b}",
            headers=_auth_headers(token),
        ) as response:
            assert response.status_code == 200
            raw = ""
            async for chunk in response.aiter_text():
                raw += chunk
        events = _parse_sse(raw)
        assert len(events) == 2
        ids = {data["document_id"] for _, data in events}
        assert ids == {doc_a, doc_b}
        assert all(name == "status" for name, _ in events)

    async def test_empty_ids_is_422(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.get(
            "/documents/stream?ids=", headers=_auth_headers(token)
        )
        assert resp.status_code == 422

    async def test_unknown_document_is_404_before_stream(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/documents/{_uuid()}/stream", headers=_auth_headers(token)
        )
        assert resp.status_code == 404

    async def test_foreign_org_document_is_404(
        self, app_client, app_session_factory
    ):
        token_a = await _register_and_login(app_client)
        user_a = await _current_user_id(app_client, token_a)
        org_a = await _get_org_id(app_session_factory, user_a)
        foreign_doc = await _seed_document_with_version(
            app_session_factory, org_a, user_a, version_status="READY"
        )
        token_b = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/documents/{foreign_doc}/stream", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 404

    async def test_multiplexed_foreign_org_id_is_404(
        self, app_client, app_session_factory
    ):
        token_a = await _register_and_login(app_client)
        user_a = await _current_user_id(app_client, token_a)
        org_a = await _get_org_id(app_session_factory, user_a)
        foreign_doc = await _seed_document_with_version(
            app_session_factory, org_a, user_a, version_status="READY"
        )
        token_b = await _register_and_login(app_client)
        user_b = await _current_user_id(app_client, token_b)
        org_b = await _get_org_id(app_session_factory, user_b)
        own_doc = await _seed_document_with_version(
            app_session_factory, org_b, user_b, version_status="READY"
        )
        # One foreign id among valid ones → the WHOLE request fails
        # (bulk never bypasses per-item authorization — §46 rule 17)
        resp = await app_client.get(
            f"/documents/stream?ids={own_doc},{foreign_doc}",
            headers=_auth_headers(token_b),
        )
        assert resp.status_code == 404


# ── Live transition (the FE §11.2 "updates live" property) ────────────────────

@pytest.mark.integration
class TestLiveTransitions:

    async def test_stream_emits_state_changes_until_terminal(
        self, app_client, app_session_factory, monkeypatch
    ):
        # Fast poll so the test does not wait on the 2 s default
        monkeypatch.setenv("STREAM_POLL_SECONDS", "0.2")
        get_settings.cache_clear()

        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        doc_id = await _seed_document_with_version(
            app_session_factory, org_id, user_id, version_status="PROCESSING"
        )

        async def _advance() -> None:
            await asyncio.sleep(0.5)
            async with app_session_factory() as session:
                await session.execute(text(
                    "UPDATE document_versions SET status = 'EMBEDDING' "
                    "WHERE document_id = :id"
                ), {"id": doc_id})
                await session.commit()
            await asyncio.sleep(0.5)
            async with app_session_factory() as session:
                await session.execute(text(
                    "UPDATE document_versions SET status = 'READY' "
                    "WHERE document_id = :id"
                ), {"id": doc_id})
                await session.commit()

        advancer = asyncio.create_task(_advance())
        try:
            async with app_client.stream(
                "GET", f"/documents/{doc_id}/stream",
                headers=_auth_headers(token),
            ) as response:
                assert response.status_code == 200
                raw = ""
                async for chunk in response.aiter_text():
                    raw += chunk
        finally:
            advancer.cancel()
            get_settings.cache_clear()

        events = _parse_sse(raw)
        statuses = [data["status"] for _, data in events]
        assert statuses[0] == "PROCESSING", "initial state arrives first"
        assert "EMBEDDING" in statuses, "live transition is emitted"
        assert statuses[-1] == "READY", "terminal state terminates the stream"
        assert len(events) >= 3
