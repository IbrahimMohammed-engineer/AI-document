"""
API tests — /conflicts (Phase 13, §27.3/§27.4; plan §23 Task 11).

Contract:
  GET  /conflicts[?status=&severity=]   → 200 (filtered, authorized list)
  GET  /conflicts/scan-status           → 200 (snapshot shape)
  GET  /conflicts/{id}                  → 200 / 404
  POST /conflicts/{id}/resolve          → 200 / 409 (already resolved) /
                                           403 (no conflict:resolve) /
                                           422 (invalid decision) / 404

Authorization matrix (§27.4 — non-negotiable):
  - a conflict backed by ANY private statement source the requester does not
    own is absent from the list and 404 on detail — always 404, never 403
    (no existence leakage);
  - cross-org conflict IDs → 404;
  - resolve re-checks source authorization live — a conflict made private
    after listing disappears (404) even for a permission holder.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.workers.jobs import run_processing_job
from tests.fixtures.conflict_fixtures import (
    HR_POLICY_A_TEXT,
    HR_POLICY_B_TEXT,
    _pair_vector,
    _unit_vector,
    install_conflict_llm_stub,
    seed_conflict_document,
    seed_conflict_org,
)


@pytest_asyncio.fixture()
async def conflicts_api_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    install_conflict_llm_stub()
    yield app_session_factory
    await pool.aclose()
    set_llm_provider(None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client, *, slug: str | None = None) -> tuple[str, str]:
    """Register org admin; return (token, org_slug)."""
    slug = slug or f"confl-api-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Conflict API Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Conflict API Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"], slug


async def _set_user_role(factory, user_id: str, org_id: str, role_name: str) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM user_roles WHERE user_id = :uid"), {"uid": user_id}
        )
        role_id = (
            await session.execute(
                text(
                    "SELECT id FROM roles "
                    "WHERE name = :name AND organization_id IS NULL AND is_system = true"
                ),
                {"name": role_name},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id, organization_id) "
                "VALUES (:uid, :rid, :oid)"
            ),
            {"uid": user_id, "rid": role_id, "oid": org_id},
        )
        await session.commit()


async def _add_org_member(
    client, factory, org_id: str, org_slug: str, email: str, role: str
) -> str:
    """Register a second user and move them into ``org_id`` with ``role``.

    Registration normally creates a new org; the user is then re-homed into
    the target org and granted a system role (the API test equivalent of an
    invite flow, bypassing Phase 16's membership endpoints).  Login uses the
    TARGET org's slug (the user's org after the move).
    """
    resp = await client.post("/auth/register", json={
        "org_name": f"Member Org {email}",
        "slug": f"member-{uuid.uuid4().hex[:8]}",
        "email": email,
        "password": "TestPassword123!",
        "full_name": "Member",
    })
    assert resp.status_code == 201, resp.text
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT id FROM users WHERE email = :email"
        ), {"email": email})).first()
        user_id = str(row[0])
        await session.execute(text(
            "UPDATE users SET organization_id = :oid WHERE id = :uid"
        ), {"oid": org_id, "uid": user_id})
        await session.commit()
    await _set_user_role(factory, user_id, org_id, role)
    login = await client.post("/auth/login", json={
        "email": email, "password": "TestPassword123!", "org_slug": org_slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _user_ids(factory, email: str) -> tuple[str, str]:
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT id, organization_id FROM users WHERE email = :email"
        ), {"email": email})).first()
        return str(row[0]), str(row[1])


async def _seed_conflict(factory, org_id: str, owner_id: str) -> str:
    """Seed the two-document corpus and run a scan → one OPEN conflict."""
    doc_a = await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy A", section_number="1",
        section_title="Vacation Approval", content=HR_POLICY_A_TEXT,
        embedding=_unit_vector(0), effective_date=date(2025, 1, 1),
    )
    await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy B", section_number="2",
        section_title="Leave Procedures", content=HR_POLICY_B_TEXT,
        embedding=_pair_vector(0, 1, cos=0.9), effective_date=date(2025, 1, 1),
    )
    from app.services.job_service import JobService

    async with factory() as session:
        job = await JobService.create_for_org_scan(session, organization_id=org_id)
        await session.commit()
        job_id = job.id
    assert await run_processing_job({}, job_id) == "COMPLETED"
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT id FROM conflicts WHERE status = 'OPEN' LIMIT 1"
        ))).first()
    assert row is not None
    return str(row[0])


# ── Contract basics ───────────────────────────────────────────────────────────

@pytest.mark.integration
class TestConflictsContract:

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.get("/conflicts")
        assert resp.status_code == 401
        resp = await app_client.post(f"/conflicts/{uuid.uuid4()}/resolve", json={
            "decision": "REVIEWED",
        })
        assert resp.status_code == 401
        resp = await app_client.get("/conflicts/scan-status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_returns_conflict_with_live_priority(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.get("/conflicts", headers=_auth_headers(token))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["status"] == "OPEN"
        assert item["detection_method"] == "BACKGROUND_SCAN"
        assert item["priority"] == "ACTIVE"  # both versions CURRENT
        assert item["statement_count"] == 2
        assert item["severity"] == "MODERATE"  # 0.9 confidence, 2 statements

    @pytest.mark.asyncio
    async def test_detail_embeds_statements_with_version_state(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.get(
            f"/conflicts/{conflict_id}", headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == conflict_id
        assert len(body["statements"]) == 2
        for statement in body["statements"]:
            assert statement["version_state"] == "CURRENT"  # computed live
            assert statement["document_name"] in ("HR Policy A", "HR Policy B")
            assert statement["effective_date"] == "2025-01-01"
            assert statement["statement_text"]
            assert statement["chunk_id"]

    @pytest.mark.asyncio
    async def test_status_and_severity_filters(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.get(
            "/conflicts", params={"status": "OPEN"}, headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

        resp = await app_client.get(
            "/conflicts", params={"status": "DISMISSED"}, headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

        resp = await app_client.get(
            "/conflicts", params={"severity": "MAJOR"}, headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

        resp = await app_client.get(
            "/conflicts", params={"status": "WRONG"}, headers=_auth_headers(token)
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_scan_status_shape(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.get(
            "/conflicts/scan-status", headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_scan_status"] == "COMPLETED"
        assert body["last_scan_completed_at"] is not None
        assert body["last_scan_conflicts_created"] == 1
        assert body["unscanned_document_count"] == 0  # scanned after seeding


# ── Resolution ────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestConflictResolution:

    @pytest.mark.asyncio
    async def test_resolve_reviewed_terminal_and_audited(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "REVIEWED", "note": "Confirmed."},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "REVIEWED"
        assert body["resolved_by"] == user_id
        assert body["resolution_note"] == "Confirmed."
        assert body["resolved_at"] is not None

        async with factory() as session:
            audits = (await session.execute(text(
                "SELECT resource_id FROM audit_logs WHERE action = 'CONFLICT_RESOLVED'"
            ))).all()
        assert len(audits) == 1
        assert str(audits[0][0]) == conflict_id

        # Terminal: resolving again → 409
        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "DISMISSED"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_decision_422(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        user_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, user_id)

        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "WHATEVER"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_viewer_cannot_resolve_403(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        admin_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, admin_id)

        viewer_token = await _add_org_member(
            app_client, factory, org_id, slug,
            f"viewer@{slug}.example.com", "Viewer",
        )
        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "DISMISSED"},
            headers=_auth_headers(viewer_token),
        )
        assert resp.status_code == 403


# ── Authorization matrix (§27.4) ──────────────────────────────────────────────

@pytest.mark.integration
class TestConflictsAuthorizationMatrix:

    @pytest.mark.asyncio
    async def test_cross_org_conflict_is_404(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token_a, slug_a = await _register_and_login(app_client)
        token_b, slug_b = await _register_and_login(app_client)
        user_a, org_a = await _user_ids(factory, f"admin@{slug_a}.example.com")
        conflict_id = await _seed_conflict(factory, org_a, user_a)

        resp = await app_client.get(
            f"/conflicts/{conflict_id}", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 404
        resp = await app_client.get(
            "/conflicts", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "REVIEWED"},
            headers=_auth_headers(token_b),
        )
        assert resp.status_code == 404  # never 403 — no existence leakage

    @pytest.mark.asyncio
    async def test_private_source_excluded_from_list_and_detail(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        admin_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, admin_id)

        member_token = await _add_org_member(
            app_client, factory, org_id, slug,
            f"member@{slug}.example.com", "Editor",
        )

        # Member sees the conflict while both sources are org-readable
        resp = await app_client.get(
            "/conflicts", headers=_auth_headers(member_token)
        )
        assert len(resp.json()["items"]) == 1

        # Make ONE statement's source private (owned by admin) → the conflict
        # becomes invisible to the member everywhere — 200-without / 404.
        async with factory() as session:
            await session.execute(text(
                "UPDATE documents SET access_level = 'private' "
                "WHERE name = 'HR Policy B'"
            ))
            await session.commit()

        resp = await app_client.get(
            "/conflicts", headers=_auth_headers(member_token)
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

        resp = await app_client.get(
            f"/conflicts/{conflict_id}", headers=_auth_headers(member_token)
        )
        assert resp.status_code == 404  # indistinguishable from nonexistent

        # Owner (admin) still sees it
        resp = await app_client.get(
            f"/conflicts/{conflict_id}", headers=_auth_headers(token)
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_resolve_with_unauthorized_source_is_404_even_with_permission(
        self, app_client: AsyncClient, app_session_factory, conflicts_api_env
    ):
        factory = conflicts_api_env
        token, slug = await _register_and_login(app_client)
        admin_id, org_id = await _user_ids(factory, f"admin@{slug}.example.com")
        conflict_id = await _seed_conflict(factory, org_id, admin_id)

        member_token = await _add_org_member(
            app_client, factory, org_id, slug,
            f"editor2@{slug}.example.com", "Editor",
        )
        async with factory() as session:
            await session.execute(text(
                "UPDATE documents SET access_level = 'private' "
                "WHERE name = 'HR Policy B'"
            ))
            await session.commit()

        # Member HAS conflict:resolve (Editor) but can't see the sources →
        # 404 (source check takes precedence — no "you could resolve it if
        # you could see it" signal).
        resp = await app_client.post(
            f"/conflicts/{conflict_id}/resolve",
            json={"decision": "DISMISSED"},
            headers=_auth_headers(member_token),
        )
        assert resp.status_code == 404
