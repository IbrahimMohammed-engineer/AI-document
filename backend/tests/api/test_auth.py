"""
API integration tests for the auth flows (Phase 2).

Runs against the real FastAPI app over an httpx ASGI client with testcontainers
PostgreSQL (full Alembic stack, seeded roles/permissions) and Redis.

Covers the verification plan from implementation_plan-phase-2.md:
  - Register → Login → /me → Refresh → Logout round-trip
  - Generic 401 on wrong password AND unknown email (no user enumeration)
  - Rate limit: exceeded logins → 429 with Retry-After
  - Refresh token rotation: original token rejected after first rotation
  - Refresh token reuse detection: replayed token revokes ALL user tokens
  - require_permission 403 matrix (live DB role re-check, JWT advisory only)
  - Cross-org isolation at the auth layer
  - Password reset: single-use token, revokes sessions, new password works
  - Audit trail rows written for key events
"""
from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import require_permission
from app.main import app
from app.models.user import User

# ─── Test-only guarded routes (require_permission matrix) ─────────────────────
# Mounted once on the app singleton to exercise permission enforcement the way
# Phase 3+ routers will declare it.

_guarded_router = APIRouter(prefix="/test-perm")


@_guarded_router.get("/user-manage")
async def _user_manage(user: Annotated[User, Depends(require_permission("user:manage"))]) -> dict:
    return {"user_id": user.id}


@_guarded_router.get("/document-read")
async def _document_read(user: Annotated[User, Depends(require_permission("document:read"))]) -> dict:
    return {"user_id": user.id}


app.include_router(_guarded_router)


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


async def _login(client: AsyncClient, *, email: str, password: str, org_slug: str) -> int:
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": password, "org_slug": org_slug},
    )
    return response.status_code


async def _set_user_role(
    factory: async_sessionmaker, user_id: str, org_id: str, role_name: str
) -> None:
    """Swap the user's role directly in the DB (bypasses any service layer)."""
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


def _new_browser() -> AsyncClient:
    """A fresh httpx client (independent cookie jar) against the same app."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _audit_actions(factory: async_sessionmaker) -> set[str]:
    async with factory() as session:
        rows = (
            await session.execute(text("SELECT DISTINCT action FROM audit_logs"))
        ).scalars()
        return set(rows)


# ─── Register / Login / Me / Refresh / Logout round-trip ──────────────────────

@pytest.mark.integration
async def test_full_auth_round_trip(app_client: AsyncClient, app_session_factory):
    body = await _register(app_client)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["access_token"]

    # Access token issued at register already works
    me = await app_client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["email"] == "admin@acme.com"
    assert me_body["full_name"] == "Ada Admin"
    assert me_body["organization"]["slug"] == "acme"
    # First user is the bootstrap Admin → full permission set
    assert "user:manage" in me_body["permissions"]
    # Full catalog: 9 base keys (migration 002) + conflict:resolve (013)
    # + summary:regenerate + extraction:create (014) = 12 keys
    assert len(me_body["permissions"]) == 12

    # Logout → new login via credentials
    logout = await app_client.post("/auth/logout")
    assert logout.status_code == 204

    login = await app_client.post(
        "/auth/login",
        json={"email": "admin@acme.com", "password": "super-secret-1", "org_slug": "acme"},
    )
    assert login.status_code == 200
    login_body = login.json()
    assert login_body["access_token"]

    # Refresh rotates the cookie and issues a new access token
    refreshed = await app_client.post("/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]

    # Final logout revokes the refresh token
    final_logout = await app_client.post("/auth/logout")
    assert final_logout.status_code == 204
    post_logout_refresh = await app_client.post("/auth/refresh")
    assert post_logout_refresh.status_code == 401

    # Audit trail contains the expected events
    actions = await _audit_actions(app_session_factory)
    assert {"USER_CREATED", "USER_LOGIN", "LOGOUT"} <= actions


@pytest.mark.integration
async def test_register_duplicate_slug_conflict(app_client: AsyncClient):
    await _register(app_client, slug="dup")
    response = await app_client.post(
        "/auth/register",
        json={
            "org_name": "Other Corp",
            "slug": "dup",
            "email": "other@corp.com",
            "full_name": "Other Person",
            "password": "another-secret-2",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


# ─── No user enumeration ──────────────────────────────────────────────────────

@pytest.mark.integration
async def test_wrong_password_and_unknown_email_return_identical_401(
    app_client: AsyncClient, app_session_factory
):
    await _register(app_client, slug="enum-guard")

    wrong_password = await app_client.post(
        "/auth/login",
        json={"email": "admin@acme.com", "password": "totally-wrong", "org_slug": "enum-guard"},
    )
    unknown_email = await app_client.post(
        "/auth/login",
        json={"email": "ghost@acme.com", "password": "totally-wrong", "org_slug": "enum-guard"},
    )
    unknown_org = await app_client.post(
        "/auth/login",
        json={"email": "admin@acme.com", "password": "totally-wrong", "org_slug": "no-such-org"},
    )

    assert wrong_password.status_code == unknown_email.status_code == unknown_org.status_code == 401
    # Enumeration-relevant fields are identical — no way to tell which part failed
    # (requestId intentionally differs per response)
    error_bodies = [
        (r.json()["error"]["code"], r.json()["error"]["message"]) for r in (wrong_password, unknown_email, unknown_org)
    ]
    assert len(set(error_bodies)) == 1
    assert error_bodies[0][0] == "INVALID_CREDENTIALS"

    # Failure is audited
    actions = await _audit_actions(app_session_factory)
    assert "LOGIN_FAILED" in actions


# ─── Rate limiting ────────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_login_rate_limit_returns_429_with_retry_after(app_client: AsyncClient):
    await _register(app_client, slug="rate-limit-org")

    for _ in range(5):
        status = await _login(
            app_client,
            email="admin@acme.com",
            password="wrong-password",
            org_slug="rate-limit-org",
        )
        assert status == 401

    blocked = await app_client.post(
        "/auth/login",
        json={"email": "admin@acme.com", "password": "wrong-password", "org_slug": "rate-limit-org"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None and int(retry_after) >= 1


# ─── Refresh token rotation + reuse detection ─────────────────────────────────

@pytest.mark.integration
async def test_refresh_rotation_rejects_original_token(app_client: AsyncClient):
    await _register(app_client, slug="rotate-org")

    first = app_client.cookies.get("refresh_token")
    assert first

    rotated = await app_client.post("/auth/refresh")
    assert rotated.status_code == 200
    second = app_client.cookies.get("refresh_token")
    assert second and second != first

    # Replaying the ORIGINAL (now revoked) token triggers reuse detection
    app_client.cookies.clear()
    replay = await app_client.post("/auth/refresh", cookies={"refresh_token": first})
    assert replay.status_code == 401

    # Reuse detection revoked ALL tokens for the user — the rotated one dies too
    app_client.cookies.clear()
    replay2 = await app_client.post("/auth/refresh", cookies={"refresh_token": second})
    assert replay2.status_code == 401


@pytest.mark.integration
async def test_refresh_reuse_detection_audited_and_revokes_all_sessions(
    app_client: AsyncClient, app_session_factory
):
    # Two independent sessions (like two browsers) for the same user
    async with _new_browser() as browser_a, _new_browser() as browser_b:
        await _register(browser_a, slug="theft-org")
        login_b = await browser_b.post(
            "/auth/login",
            json={"email": "admin@acme.com", "password": "super-secret-1", "org_slug": "theft-org"},
        )
        assert login_b.status_code == 200

        # Browser A refreshes normally — its original token (stolen below) is
        # revoked by the rotation
        stolen = browser_a.cookies.get("refresh_token")
        rotated = await browser_a.post("/auth/refresh")
        assert rotated.status_code == 200

        # Attacker replays the stolen (now revoked) token
        replay = await browser_a.post("/auth/refresh", cookies={"refresh_token": stolen})
        assert replay.status_code == 401

        # Theft detected → every session for the user is revoked
        innocent = await browser_b.post("/auth/refresh")
        assert innocent.status_code == 401

    actions = await _audit_actions(app_session_factory)
    assert "TOKEN_REVOKED_REUSE_DETECTED" in actions


# ─── require_permission enforcement (live DB check) ───────────────────────────

@pytest.mark.integration
@pytest.mark.parametrize(
    ("endpoint", "role", "expected_status"),
    [
        ("/test-perm/user-manage", "Admin", 200),
        ("/test-perm/user-manage", "Viewer", 403),
        ("/test-perm/document-read", "Admin", 200),
        ("/test-perm/document-read", "Viewer", 200),
    ],
)
async def test_require_permission_matrix(
    app_client: AsyncClient, app_session_factory, endpoint: str, role: str, expected_status: int
):
    body = await _register(app_client, slug=f"perm-{role}-{endpoint.replace('/', '')}".lower())

    me = await app_client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    me_body = me.json()
    user_id = me_body["id"]
    org_id = me_body["organization"]["id"]

    # Swap the role AFTER the JWT was issued — enforcement must follow the DB,
    # not the (now stale) advisory roles claim in the token.
    await _set_user_role(app_session_factory, user_id, org_id, role)

    response = await app_client.get(
        endpoint, headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["error"]["code"] == "INSUFFICIENT_PERMISSIONS"


# ─── Current-user resolution failures ─────────────────────────────────────────

@pytest.mark.integration
async def test_me_requires_valid_token(app_client: AsyncClient):
    no_token = await app_client.get("/auth/me")
    assert no_token.status_code == 401

    garbage = await app_client.get("/auth/me", headers={"Authorization": "Bearer garbage.token.here"})
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "TOKEN_INVALID"

    body = await _register(app_client, slug="me-guard")
    tampered = body["access_token"][:-4] + "AAAA"
    bad_signature = await app_client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})
    assert bad_signature.status_code == 401


@pytest.mark.integration
async def test_refresh_without_cookie_returns_401(app_client: AsyncClient):
    response = await app_client.post("/auth/refresh")
    assert response.status_code == 401


# ─── Multi-tenant isolation at the auth layer ─────────────────────────────────

@pytest.mark.integration
async def test_same_email_in_two_orgs_is_isolated(app_client: AsyncClient):
    # The same email can exist in two tenants with different credentials
    await _register(app_client, slug="org-a", email="shared@users.com", password="password-a-123")
    await _register(
        app_client,
        org_name="Org B",
        slug="org-b",
        email="shared@users.com",
        password="password-b-456",
        full_name="Ben B",
    )

    assert await _login(app_client, email="shared@users.com", password="password-a-123", org_slug="org-a") == 200
    # Org A credentials never authenticate against org B (and vice versa)
    assert await _login(app_client, email="shared@users.com", password="password-a-123", org_slug="org-b") == 401
    assert await _login(app_client, email="shared@users.com", password="password-b-456", org_slug="org-a") == 401

    login = await app_client.post(
        "/auth/login",
        json={"email": "shared@users.com", "password": "password-b-456", "org_slug": "org-b"},
    )
    assert login.status_code == 200
    me = await app_client.get(
        "/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["organization"]["slug"] == "org-b"
    assert me.json()["full_name"] == "Ben B"


# ─── Password reset ───────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_password_reset_flow_single_use_and_revokes_sessions(
    app_client: AsyncClient, app_session_factory
):
    # Session 1 (the requester) and session 2 (another browser)
    async with _new_browser() as requester, _new_browser() as other_session:
        await _register(requester, slug="reset-org")
        login_other = await other_session.post(
            "/auth/login",
            json={"email": "admin@acme.com", "password": "super-secret-1", "org_slug": "reset-org"},
        )
        assert login_other.status_code == 200

        forgot = await requester.post(
            "/auth/forgot-password",
            json={"email": "admin@acme.com", "org_slug": "reset-org"},
        )
        assert forgot.status_code == 200
        reset_token = forgot.json()["reset_token"]
        assert reset_token  # dev mode returns the token in the body

        reset = await requester.post(
            "/auth/reset-password",
            json={"token": reset_token, "new_password": "brand-new-secret-9"},
        )
        assert reset.status_code == 204

        # Old password no longer works; new one does
        assert await _login(
            requester, email="admin@acme.com", password="super-secret-1", org_slug="reset-org"
        ) == 401
        assert await _login(
            requester, email="admin@acme.com", password="brand-new-secret-9", org_slug="reset-org"
        ) == 200

        # Reset token is single-use
        replay = await requester.post(
            "/auth/reset-password",
            json={"token": reset_token, "new_password": "another-secret-8"},
        )
        assert replay.status_code == 401

        # Password reset revoked every refresh token for the user
        stale_session = await other_session.post("/auth/refresh")
        assert stale_session.status_code == 401

    actions = await _audit_actions(app_session_factory)
    assert {"PASSWORD_RESET_REQUESTED", "PASSWORD_RESET_COMPLETED"} <= actions


@pytest.mark.integration
async def test_forgot_password_unknown_account_is_opaque(app_client: AsyncClient):
    response = await app_client.post(
        "/auth/forgot-password",
        json={"email": "nobody@nowhere.com", "org_slug": "ghost-org"},
    )
    # Same 200 + generic message — no account enumeration
    assert response.status_code == 200
    assert response.json()["reset_token"] is None
