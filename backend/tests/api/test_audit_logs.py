"""
API integration tests for the audit-log read endpoint (Phase 15).

Covers:
  - Org isolation (two orgs, events in both — only the caller's org returned)
  - Action / date-range filters
  - Pagination window (limit/offset)
  - 403 for non-admin, 401 for unauthenticated
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import app

# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _register(
    client: AsyncClient,
    *,
    slug: str = "acme",
    email: str = "admin@acme.com",
    org_name: str = "Acme Corp",
) -> dict:
    response = await client.post(
        "/auth/register",
        json={
            "org_name": org_name,
            "slug": slug,
            "email": email,
            "full_name": "Ada Admin",
            "password": "super-secret-1",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_audit(
    factory: async_sessionmaker,
    *,
    org_id: str,
    actions: list[str],
    at: datetime | None = None,
) -> None:
    async with factory() as session:
        for i, action in enumerate(actions):
            if at is not None:
                created = (at + timedelta(seconds=i)).isoformat()
                created_sql = f"'{created}'"
            else:
                created_sql = "now()"
            await session.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(organization_id, user_id, action, resource_type, metadata, created_at) "
                    "VALUES (:oid, NULL, :action, 'document', '{}', "
                    f"{created_sql}"
                    ")"
                ),
                {"oid": org_id, "action": action},
            )
        await session.commit()


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_audit_logs_org_isolation(app_client: AsyncClient, app_session_factory):
    token = (await _register(app_client))["access_token"]
    other_token = (
        await _register(
            app_client,
            slug="beta",
            email="admin@beta.com",
            org_name="Beta LLC",
        )
    )["access_token"]

    me_a = (await app_client.get("/auth/me", headers=_auth(token))).json()
    me_b = (await app_client.get("/auth/me", headers=_auth(other_token))).json()

    # Baseline totals (registration itself writes USER_CREATED events)
    base_a = (await app_client.get("/audit-logs", headers=_auth(token))).json()["total"]
    base_b = (await app_client.get("/audit-logs", headers=_auth(other_token))).json()["total"]

    await _seed_audit(
        app_session_factory, org_id=me_a["organization"]["id"], actions=["DOCUMENT_UPLOADED", "USER_LOGIN"]
    )
    await _seed_audit(
        app_session_factory, org_id=me_b["organization"]["id"], actions=["CONFLICT_RESOLVED"]
    )

    response_a = await app_client.get("/audit-logs", headers=_auth(token))
    assert response_a.status_code == 200
    body_a = response_a.json()
    assert body_a["total"] == base_a + 2
    assert {item["action"] for item in body_a["items"]} >= {"DOCUMENT_UPLOADED", "USER_LOGIN"}

    response_b = await app_client.get("/audit-logs", headers=_auth(other_token))
    assert response_b.json()["total"] == base_b + 1
    assert response_b.json()["items"][0]["action"] == "CONFLICT_RESOLVED"


@pytest.mark.integration
async def test_audit_logs_action_filter(app_client: AsyncClient, app_session_factory):
    token = (await _register(app_client))["access_token"]
    me = (await app_client.get("/auth/me", headers=_auth(token))).json()

    await _seed_audit(
        app_session_factory,
        org_id=me["organization"]["id"],
        actions=["DOCUMENT_UPLOADED", "USER_LOGIN", "DOCUMENT_UPLOADED"],
    )

    response = await app_client.get(
        "/audit-logs?action=DOCUMENT_UPLOADED", headers=_auth(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(item["action"] == "DOCUMENT_UPLOADED" for item in body["items"])


@pytest.mark.integration
async def test_audit_logs_date_range_filter(app_client: AsyncClient, app_session_factory):
    token = (await _register(app_client))["access_token"]
    me = (await app_client.get("/auth/me", headers=_auth(token))).json()
    org_id = me["organization"]["id"]

    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=60)
    recent = now - timedelta(days=15)

    await _seed_audit(app_session_factory, org_id=org_id, actions=["USER_LOGIN"], at=old)
    await _seed_audit(app_session_factory, org_id=org_id, actions=["USER_LOGIN"], at=recent)

    frm = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Filter by action too — registration itself wrote a USER_CREATED row at
    # ~now, which would otherwise also fall inside the range.
    response = await app_client.get(
        f"/audit-logs?from={frm}&action=USER_LOGIN", headers=_auth(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "USER_LOGIN"


@pytest.mark.integration
async def test_audit_logs_pagination(app_client: AsyncClient, app_session_factory):
    token = (await _register(app_client))["access_token"]
    me = (await app_client.get("/auth/me", headers=_auth(token))).json()

    # Baseline (registration writes its own USER_CREATED event)
    base = (await app_client.get("/audit-logs", headers=_auth(token))).json()["total"]
    await _seed_audit(
        app_session_factory,
        org_id=me["organization"]["id"],
        actions=[f"EVENT_{i:02d}" for i in range(12)],
    )

    page = await app_client.get("/audit-logs?limit=5&offset=5", headers=_auth(token))
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == base + 12
    assert len(body["items"]) == 5


@pytest.mark.integration
async def test_audit_logs_forbidden_for_non_admin(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    me = (await app_client.get("/auth/me", headers=_auth(token))).json()
    org_id = me["organization"]["id"]

    # Demote the bootstrap admin to Viewer
    async with app_session_factory() as session:
        await session.execute(
            text("DELETE FROM user_roles WHERE organization_id = :oid"), {"oid": org_id}
        )
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = 'Viewer' AND organization_id IS NULL")
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id, organization_id) "
                "VALUES (:uid, :rid, :oid)"
            ),
            {"uid": me["id"], "rid": role_id, "oid": org_id},
        )
        await session.commit()

    response = await app_client.get("/audit-logs", headers=_auth(token))
    assert response.status_code == 403


@pytest.mark.integration
async def test_audit_logs_requires_auth(app_client: AsyncClient):
    response = await app_client.get("/audit-logs")
    assert response.status_code == 401
