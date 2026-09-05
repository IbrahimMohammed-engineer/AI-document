"""
API integration tests for the Settings endpoints (Phase 15).

Runs against the real FastAPI app over an httpx ASGI client with testcontainers
PostgreSQL (full Alembic stack, seeded roles/permissions) and Redis.

Covers:
  - PATCH /settings/profile (own profile, refreshed /me shape)
  - PATCH /settings/organization (settings:manage gate, slug immutable)
  - GET /settings/users (user:manage gate, pagination, org isolation)
  - PATCH /settings/users/{id}/roles (role swap, self-lockout 422, audit)
  - GET /settings/roles (system + org roles with permission keys)
  - 403 matrix for non-admin users, 401 for unauthenticated requests
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import app

# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _register(
    client: AsyncClient,
    *,
    org_name: str = "Acme Corp",
    slug: str = "acme",
    email: str = "admin@acme.com",
    password: str = "super-secret-1",
    full_name: str = "Ada Admin",
) -> dict:
    response = await client.post(
        "/auth/register",
        json={
            "org_name": org_name,
            "slug": slug,
            "email": email,
            "full_name": full_name,
            "password": password,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _add_member(
    factory: async_sessionmaker,
    *,
    org_id: str,
    email: str,
    full_name: str,
    password_hash: str = "x",
    role_name: str | None = "Viewer",
) -> str:
    """Insert a user directly (register would create its own org)."""
    async with factory() as session:
        user_id = (
            await session.execute(
                text(
                    "INSERT INTO users (organization_id, email, full_name, password_hash) "
                    "VALUES (:oid, :email, :name, 'x') RETURNING id"
                ),
                {"oid": org_id, "email": email, "name": full_name},
            )
        ).scalar_one()
        if role_name:
            role_id = (
                await session.execute(
                    text(
                        "SELECT id FROM roles WHERE name = :name "
                        "AND organization_id IS NULL AND is_system = true"
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
        return str(user_id)


def _new_browser() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _system_role_ids(factory: async_sessionmaker) -> dict[str, str]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT name, id FROM roles "
                "WHERE organization_id IS NULL AND is_system = true"
            )
        )
        return {name: str(role_id) for name, role_id in rows.all()}


# ─── PATCH /settings/profile ──────────────────────────────────────────────────


@pytest.mark.integration
async def test_update_profile_round_trip(app_client: AsyncClient):
    token = (await _register(app_client))["access_token"]

    response = await app_client.patch(
        "/settings/profile",
        json={"full_name": "Ada Lovelace"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["full_name"] == "Ada Lovelace"
    assert body["email"] == "admin@acme.com"
    assert "user:manage" in body["permissions"]

    # /auth/me reflects the change too
    me = await app_client.get("/auth/me", headers=_auth(token))
    assert me.json()["full_name"] == "Ada Lovelace"


@pytest.mark.integration
async def test_update_profile_rejects_short_name(app_client: AsyncClient):
    token = (await _register(app_client))["access_token"]
    response = await app_client.patch(
        "/settings/profile", json={"full_name": "A"}, headers=_auth(token)
    )
    assert response.status_code == 422


@pytest.mark.integration
async def test_update_profile_requires_auth(app_client: AsyncClient):
    response = await app_client.patch("/settings/profile", json={"full_name": "New Name"})
    assert response.status_code == 401


# ─── PATCH /settings/organization ─────────────────────────────────────────────


@pytest.mark.integration
async def test_update_organization_name(app_client: AsyncClient):
    token = (await _register(app_client))["access_token"]

    response = await app_client.patch(
        "/settings/organization",
        json={"name": "Acme Industries"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Acme Industries"
    assert body["slug"] == "acme"  # slug is immutable


@pytest.mark.integration
async def test_update_organization_forbidden_for_viewer(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    org_id = (await app_client.get("/auth/me", headers=_auth(token))).json()["organization"]["id"]

    # A password-capable Viewer member of the same org
    from app.core.security import hash_password

    await _add_member(
        app_session_factory, org_id=org_id, email="v@acme.com", full_name="Vee Viewer"
    )
    async with app_session_factory() as session:
        await session.execute(
            text("UPDATE users SET password_hash = :h WHERE email = 'v@acme.com'"),
            {"h": hash_password("viewer-pass-1")},
        )
        await session.commit()

    login = await app_client.post(
        "/auth/login",
        json={"email": "v@acme.com", "password": "viewer-pass-1", "org_slug": "acme"},
    )
    assert login.status_code == 200
    viewer_token = login.json()["access_token"]

    response = await app_client.patch(
        "/settings/organization",
        json={"name": "Renamed"},
        headers=_auth(viewer_token),
    )
    assert response.status_code == 403


@pytest.mark.integration
async def test_update_organization_requires_auth(app_client: AsyncClient):
    response = await app_client.patch("/settings/organization", json={"name": "X Y"})
    assert response.status_code == 401


# ─── GET /settings/users ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_list_users_pagination_and_isolation(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    org_id = (await app_client.get("/auth/me", headers=_auth(token))).json()["organization"]["id"]

    for i in range(7):
        await _add_member(
            app_session_factory,
            org_id=org_id,
            email=f"member{i}@acme.com",
            full_name=f"Member {i:02d}",
        )

    # Second org with its own member — must never leak
    other_token = (
        await _register(
            app_client,
            org_name="Beta LLC",
            slug="beta",
            email="admin@beta.com",
        )
    )["access_token"]

    response = await app_client.get("/settings/users?limit=5&offset=0", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 8  # admin + 7 members
    assert len(body["items"]) == 5
    assert all(item["email"].endswith("@acme.com") for item in body["items"])
    assert all(isinstance(item["roles"], list) for item in body["items"])

    # Page 2
    page2 = await app_client.get("/settings/users?limit=5&offset=5", headers=_auth(token))
    assert len(page2.json()["items"]) == 3

    # Org B's admin cannot see org A's users
    other = await app_client.get("/settings/users", headers=_auth(other_token))
    assert other.status_code == 200
    assert other.json()["total"] == 1
    assert other.json()["items"][0]["email"] == "admin@beta.com"


@pytest.mark.integration
async def test_list_users_forbidden_for_non_admin(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    org_id = (await app_client.get("/auth/me", headers=_auth(token))).json()["organization"]["id"]
    await _add_member(
        app_session_factory, org_id=org_id, email="v@acme.com", full_name="Vee Viewer"
    )
    # Demote the admin to Viewer for the 403 check
    async with app_session_factory() as session:
        await session.execute(text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE email = 'admin@acme.com')"))
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = 'Viewer' AND organization_id IS NULL")
            )
        ).scalar_one()
        admin_id = (
            await session.execute(text("SELECT id FROM users WHERE email = 'admin@acme.com'"))
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id, organization_id) VALUES (:uid, :rid, :oid)"
            ),
            {"uid": admin_id, "rid": role_id, "oid": org_id},
        )
        await session.commit()

    response = await app_client.get("/settings/users", headers=_auth(token))
    assert response.status_code == 403


# ─── PATCH /settings/users/{id}/roles ─────────────────────────────────────────


@pytest.mark.integration
async def test_update_user_roles_round_trip(app_client: AsyncClient, app_session_factory):
    token = (await _register(app_client))["access_token"]
    org_id = (await app_client.get("/auth/me", headers=_auth(token))).json()["organization"]["id"]
    member_id = await _add_member(
        app_session_factory, org_id=org_id, email="m@acme.com", full_name="Mia Member"
    )
    roles = await _system_role_ids(app_session_factory)

    response = await app_client.patch(
        f"/settings/users/{member_id}/roles",
        json={"role_ids": [roles["Editor"]]},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["roles"] == ["Editor"]
    assert body["role_ids"] == [roles["Editor"]]

    # Audit event written
    async with app_session_factory() as session:
        action = (
            await session.execute(
                text(
                    "SELECT action FROM audit_logs "
                    "WHERE action = 'PERMISSION_CHANGED' AND resource_id = :rid"
                ),
                {"rid": member_id},
            )
        ).scalar_one_or_none()
    assert action == "PERMISSION_CHANGED"


@pytest.mark.integration
async def test_update_user_roles_rejects_cross_org_role(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    org_id = (await app_client.get("/auth/me", headers=_auth(token))).json()["organization"]["id"]
    member_id = await _add_member(
        app_session_factory, org_id=org_id, email="m@acme.com", full_name="Mia Member"
    )

    # An org-B custom role id must be rejected
    async with app_session_factory() as session:
        other_org_id = (
            await session.execute(
                text(
                    "INSERT INTO organizations (name, slug) "
                    "VALUES ('Gamma Inc', 'gamma') RETURNING id"
                )
            )
        ).scalar_one()
        foreign_role_id = (
            await session.execute(
                text(
                    "INSERT INTO roles (organization_id, name, is_system) "
                    "VALUES (:oid, 'Gamma Custom', false) RETURNING id"
                ),
                {"oid": str(other_org_id)},
            )
        ).scalar_one()
        await session.commit()

    response = await app_client.patch(
        f"/settings/users/{member_id}/roles",
        json={"role_ids": [str(foreign_role_id)]},
        headers=_auth(token),
    )
    assert response.status_code == 422


@pytest.mark.integration
async def test_update_own_roles_self_lockout_guard(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    me = (await app_client.get("/auth/me", headers=_auth(token))).json()
    roles = await _system_role_ids(app_session_factory)

    # Attempt to demote self to Viewer — must be blocked with 422
    response = await app_client.patch(
        f"/settings/users/{me['id']}/roles",
        json={"role_ids": [roles["Viewer"]]},
        headers=_auth(token),
    )
    assert response.status_code == 422


@pytest.mark.integration
async def test_update_user_roles_unknown_user_404(
    app_client: AsyncClient, app_session_factory
):
    token = (await _register(app_client))["access_token"]
    response = await app_client.patch(
        "/settings/users/00000000-0000-0000-0000-000000000000/roles",
        json={"role_ids": ["00000000-0000-0000-0000-000000000001"]},
        headers=_auth(token),
    )
    assert response.status_code == 404


# ─── GET /settings/roles ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_list_roles_returns_system_roles(app_client: AsyncClient):
    token = (await _register(app_client))["access_token"]
    response = await app_client.get("/settings/roles", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    names = {item["name"] for item in body["items"]}
    assert {"Admin", "Editor", "Viewer"} <= names
    admin = next(item for item in body["items"] if item["name"] == "Admin")
    assert "user:manage" in admin["permissions"]
    assert admin["is_system"] is True
