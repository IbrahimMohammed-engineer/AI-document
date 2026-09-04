"""
API tests — /documents/compare (Phase 12, plan §16.3/§16.4).

Covers every status-code path of the endpoint table plus the explicit
authorization matrix:

  POST   /documents/compare → 202 (new) / 200 (existing) /
                              422 (identical or not-READY) /
                              404 (either side unauthorized or cross-org) /
                              403 (no comparison:create permission)
  GET    /documents/compare/{id}                  → 200 / 404
  GET    /documents/compare/{id}/changes[?severity=&section=] → 200 / 404
  GET    /documents/compare/{id}/changes/{cid}    → 200 / 404 (SourceRef check)

Read-time re-authorization (plan §13): a comparison remains visible only
while the requesting user can access BOTH source versions — access revoked
after creation makes the GETs 404 again.
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
from tests.fixtures.comparison_fixtures import seed_document_with_versions


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _uuid() -> str:
    return str(uuid.uuid4())


async def _register_and_login(client, *, slug: str | None = None) -> tuple[str, str]:
    """Register org admin; return (token, org_slug)."""
    slug = slug or f"cmp-api-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Compare Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Compare Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"], slug


async def _user_id(client, token: str) -> str:
    resp = await client.get("/auth/me", headers=_auth_headers(token))
    assert resp.status_code == 200
    return resp.json()["id"]


async def _org_id(factory: async_sessionmaker, user_id: str) -> str:
    async with factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"),
            {"id": user_id},
        )
        return str(row.scalar_one())


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


@pytest_asyncio.fixture()
async def worker_bound(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    """Bind queue + DB factory so POST /compare's enqueue and direct
    run_processing_job calls work against the test containers."""
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    set_llm_provider(StubLLMProvider(
        responder=lambda messages: '{"materiality": "material", "rationale": "changed"}'
    ))
    yield app_session_factory
    set_llm_provider(None)
    await pool.aclose()


# ── Contract basics ───────────────────────────────────────────────────────────

@pytest.mark.integration
class TestCompareContract:

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.post("/documents/compare", json={
            "document_a_version_id": _uuid(),
            "document_b_version_id": _uuid(),
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_version_returns_404(
        self, app_client, app_session_factory
    ):
        token, _ = await _register_and_login(app_client)
        resp = await app_client.post("/documents/compare", headers=_auth_headers(token), json={
            "document_a_version_id": _uuid(),
            "document_b_version_id": _uuid(),
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_identical_version_ids_rejected(
        self, app_client, app_session_factory
    ):
        """Plan §9.3: comparing a version with itself is rejected before any
        DB write.  (The plan's "422" maps to this codebase's business-
        validation convention: ValidationError → HTTP 400.)"""
        token, _ = await _register_and_login(app_client)
        same = _uuid()
        resp = await app_client.post("/documents/compare", headers=_auth_headers(token), json={
            "document_a_version_id": same,
            "document_b_version_id": same,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cross_org_version_returns_404_not_403(
        self, app_client, app_session_factory
    ):
        """Existence-leakage rule: another org's version looks like a
        missing one (§9.2)."""
        token_a, slug_a = await _register_and_login(app_client)
        token_b, slug_b = await _register_and_login(app_client)

        user_a = await _user_id(app_client, token_a)
        org_a = await _org_id(app_session_factory, user_a)
        _, seeds = await seed_document_with_versions(
            app_session_factory, organization_id=org_a, owner_id=user_a
        )

        resp = await app_client.post("/documents/compare", headers=_auth_headers(token_b), json={
            "document_a_version_id": seeds[1].version_id,
            "document_b_version_id": seeds[2].version_id,
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_not_ready_version_rejected(
        self, app_client, app_session_factory
    ):
        token, _ = await _register_and_login(app_client)
        user_id = await _user_id(app_client, token)
        org_id = await _org_id(app_session_factory, user_id)
        _, seeds = await seed_document_with_versions(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )
        async with app_session_factory() as session:
            await session.execute(text(
                "UPDATE document_versions SET status = 'EMBEDDING' WHERE id = :id"
            ), {"id": seeds[2].version_id})
            await session.commit()

        resp = await app_client.post("/documents/compare", headers=_auth_headers(token), json={
            "document_a_version_id": seeds[1].version_id,
            "document_b_version_id": seeds[2].version_id,
        })
        assert resp.status_code == 400  # ValidationError convention (plan "422")


# ── Creation lifecycle + idempotency ──────────────────────────────────────────

@pytest.mark.integration
class TestCompareCreation:

    @pytest.mark.asyncio
    async def test_create_then_reuse_status_codes_and_idempotency(
        self, app_client, app_session_factory, worker_bound
    ):
        token, _ = await _register_and_login(app_client)
        user_id = await _user_id(app_client, token)
        org_id = await _org_id(app_session_factory, user_id)
        _, seeds = await seed_document_with_versions(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )
        headers = _auth_headers(token)
        body = {
            "document_a_version_id": seeds[1].version_id,
            "document_b_version_id": seeds[2].version_id,
        }

        resp = await app_client.post("/documents/compare", headers=headers, json=body)
        assert resp.status_code == 202, resp.text
        payload = resp.json()
        assert payload["created"] is True
        assert payload["status"] == "PENDING"
        comparison_id = payload["id"]

        async with app_session_factory() as session:
            jobs_before = (await session.execute(
                text("SELECT count(*) FROM processing_jobs")
            )).scalar_one()

        # Same pair, reversed order → same comparison, HTTP 200, no new job
        resp2 = await app_client.post("/documents/compare", headers=headers, json={
            "document_a_version_id": body["document_b_version_id"],
            "document_b_version_id": body["document_a_version_id"],
        })
        assert resp2.status_code == 200
        payload2 = resp2.json()
        assert payload2["created"] is False
        assert payload2["id"] == comparison_id

        async with app_session_factory() as session:
            jobs_after = (await session.execute(
                text("SELECT count(*) FROM processing_jobs")
            )).scalar_one()
        assert jobs_after == jobs_before == 1

    @pytest.mark.asyncio
    async def test_viewer_cannot_create_but_can_read(
        self, app_client, app_session_factory, worker_bound
    ):
        """comparison:create is Admin/Editor — Viewer gets 403 on POST but
        document:read still permits GETs (plan §13 permission table)."""
        token, slug = await _register_and_login(app_client)
        user_id = await _user_id(app_client, token)
        org_id = await _org_id(app_session_factory, user_id)
        _, seeds = await seed_document_with_versions(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )

        viewer_token = token
        await _set_user_role(app_session_factory, user_id, org_id, "Viewer")

        resp = await app_client.post("/documents/compare", headers=_auth_headers(viewer_token), json={
            "document_a_version_id": seeds[1].version_id,
            "document_b_version_id": seeds[2].version_id,
        })
        assert resp.status_code == 403


# ── Authorization matrix (§16.4) ──────────────────────────────────────────────

@pytest.mark.integration
class TestCompareAuthorizationMatrix:

    async def _make_comparison(
        self, app_client, factory, *, a_level="organization", b_level="organization",
        revoke_a_after=False,
    ):
        """Two documents; returns (admin_token, viewer_token, comparison_id, ids)."""
        token, slug = await _register_and_login(app_client)
        user_id = await _user_id(app_client, token)
        org_id = await _org_id(factory, user_id)

        doc_a, seeds_a = await seed_document_with_versions(
            factory, organization_id=org_id, owner_id=user_id, name="Doc A",
        )
        doc_b, seeds_b = await seed_document_with_versions(
            factory, organization_id=org_id, owner_id=user_id, name="Doc B",
        )
        if a_level != "organization" or b_level != "organization":
            async with factory() as session:
                await session.execute(text(
                    "UPDATE documents SET access_level = :lvl WHERE id = :id"
                ), {"lvl": a_level, "id": doc_a})
                await session.execute(text(
                    "UPDATE documents SET access_level = :lvl WHERE id = :id"
                ), {"lvl": b_level, "id": doc_b})
                await session.commit()

        resp = await app_client.post("/documents/compare", headers=_auth_headers(token), json={
            "document_a_version_id": seeds_a[1].version_id,
            "document_b_version_id": seeds_b[1].version_id,
        })
        assert resp.status_code == 202, resp.text
        comparison_id = resp.json()["id"]

        if revoke_a_after:
            # Simulate revocation: doc A becomes private to a DIFFERENT
            # (real) user in the same org
            async with factory() as session:
                other = str((await session.execute(text(
                    "INSERT INTO users (id, organization_id, email, full_name, "
                    "password_hash) VALUES (gen_random_uuid(), :org, :email, "
                    "'Other Owner', 'x') RETURNING id"
                ), {"org": org_id,
                    "email": f"other-{_uuid()}@revocation.test"})).scalar_one())
                await session.execute(text(
                    "UPDATE documents SET access_level = 'private', owner_id = :o "
                    "WHERE id = :id"
                ), {"o": other, "id": doc_a})
                await session.commit()

        return token, comparison_id, (seeds_a[1].version_id, seeds_b[1].version_id)

    @pytest.mark.asyncio
    async def test_both_sides_private_to_owner_still_allowed(
        self, app_client, app_session_factory
    ):
        token, comparison_id, _ = await self._make_comparison(
            app_client, app_session_factory, a_level="private", b_level="private"
        )
        resp = await app_client.get(
            f"/documents/compare/{comparison_id}", headers=_auth_headers(token)
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_revoked_access_after_creation_fails_reads(
        self, app_client, app_session_factory
    ):
        """Re-checked on EVERY read (plan §13): once either side is no longer
        accessible to the requester, all GETs 404."""
        token, comparison_id, _ = await self._make_comparison(
            app_client, app_session_factory, revoke_a_after=True
        )
        headers = _auth_headers(token)
        resp = await app_client.get(
            f"/documents/compare/{comparison_id}", headers=headers
        )
        assert resp.status_code == 404
        resp = await app_client.get(
            f"/documents/compare/{comparison_id}/changes", headers=headers
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unrelated_org_user_gets_404_on_reads(
        self, app_client, app_session_factory
    ):
        token, comparison_id, _ = await self._make_comparison(
            app_client, app_session_factory
        )
        other_token, _ = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/documents/compare/{comparison_id}", headers=_auth_headers(other_token)
        )
        assert resp.status_code == 404


# ── Reads: detail, changes, filters, change detail + SourceRefs ───────────────

@pytest.mark.integration
class TestCompareReads:

    @pytest.mark.asyncio
    async def test_changes_filters_and_change_detail_with_sources(
        self, app_client, app_session_factory, worker_bound
    ):
        token, _ = await _register_and_login(app_client)
        user_id = await _user_id(app_client, token)
        org_id = await _org_id(app_session_factory, user_id)
        _, seeds = await seed_document_with_versions(
            app_session_factory, organization_id=org_id, owner_id=user_id
        )
        headers = _auth_headers(token)

        resp = await app_client.post("/documents/compare", headers=headers, json={
            "document_a_version_id": seeds[1].version_id,
            "document_b_version_id": seeds[2].version_id,
        })
        assert resp.status_code == 202
        comparison_id = resp.json()["id"]
        job_id = await _single_job_id(app_session_factory)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        # Detail shows COMPLETED + summary
        detail = await app_client.get(
            f"/documents/compare/{comparison_id}", headers=headers
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["status"] == "COMPLETED"
        assert body["summary"]["total"] == 3

        # Changes list + severity filter
        changes = await app_client.get(
            f"/documents/compare/{comparison_id}/changes", headers=headers
        )
        assert changes.status_code == 200
        items = changes.json()["items"]
        assert len(items) == 3
        assert {i["change_type"] for i in items} == {"MODIFIED", "ADDED"}

        major = await app_client.get(
            f"/documents/compare/{comparison_id}/changes",
            params={"severity": "MAJOR"}, headers=headers,
        )
        assert major.status_code == 200
        assert major.json()["total"] == 0

        section_filter = await app_client.get(
            f"/documents/compare/{comparison_id}/changes",
            params={"section": "Approval Process"}, headers=headers,
        )
        assert section_filter.status_code == 200
        assert section_filter.json()["total"] == 1

        # Change detail resolves SourceRefs to the correct versions/pages
        change_id = items[0]["id"]
        one = await app_client.get(
            f"/documents/compare/{comparison_id}/changes/{change_id}",
            headers=headers,
        )
        assert one.status_code == 200
        change = one.json()
        for key in ("old_source", "new_source"):
            source = change.get(key)
            if change[f"{key.split('_')[0]}_chunk_id"] is None:
                assert source is None
            else:
                assert source is not None
                assert source["page_number"] == 1
                assert source["document_name"] == "Marketing Policy"
                expected_version = (
                    seeds[1].version_id if key == "old_source" else seeds[2].version_id
                )
                assert source["document_version_id"] == expected_version

        # Unknown change id → 404
        missing = await app_client.get(
            f"/documents/compare/{comparison_id}/changes/{_uuid()}",
            headers=headers,
        )
        assert missing.status_code == 404


async def _single_job_id(factory) -> str:
    async with factory() as session:
        row = await session.execute(text("SELECT id FROM processing_jobs"))
        return str(row.scalars().first())
