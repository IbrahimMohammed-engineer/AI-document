"""
Unit tests for pure permission evaluation (app/domain/permissions.py).

Covers all 3 system roles × all permission keys plus edge cases
(no roles, multiple roles). The role→permission assignments asserted here
mirror the seeds from migrations 002 (base keys), 013 (conflict:resolve),
and 014 (summary:regenerate + extraction:create — Admin/Editor only)
exactly.
"""
from __future__ import annotations

import pytest

from app.domain.permissions import (
    PermissionKey,
    get_user_permissions,
    has_permission,
)
import app.models.organization  # noqa: F401 — registers Organization for mapper resolution
from app.models.user import Permission, Role

ALL_KEYS = [key.value for key in PermissionKey]

# Mirrors migration 002 _seed_system_roles() + migration 013
# _seed_conflict_permission() + migration 014 _seed_phase14_permissions()
# (Admin/Editor only — never Viewer)
ROLE_PERMISSIONS = {
    "Admin": {
        "document:create",
        "document:read",
        "document:update",
        "document:delete",
        "chat:create",
        "comparison:create",
        "conflict:resolve",
        "summary:regenerate",
        "extraction:create",
        "user:manage",
        "settings:manage",
        "analytics:read",
    },
    "Editor": {
        "document:create",
        "document:read",
        "document:update",
        "document:delete",
        "chat:create",
        "comparison:create",
        "conflict:resolve",
        "summary:regenerate",
        "extraction:create",
        "analytics:read",
    },
    "Viewer": {
        "document:read",
        "chat:create",
    },
}


def make_role(name: str, keys: set[str]) -> Role:
    role = Role(name=name)
    role.permissions = [Permission(key=key) for key in keys]
    return role


def system_role(name: str) -> Role:
    return make_role(name, ROLE_PERMISSIONS[name])


# ─── PermissionKey catalog ────────────────────────────────────────────────────

@pytest.mark.unit
def test_permission_key_catalog_has_twelve_entries():
    assert len(list(PermissionKey)) == 12
    assert set(k.value for k in PermissionKey) == set(ALL_KEYS)


# ─── System role × permission key matrix ─────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("key", ALL_KEYS)
def test_admin_has_all_permissions(key: str):
    assert has_permission([system_role("Admin")], key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    sorted(ROLE_PERMISSIONS["Editor"]),
)
def test_editor_grants(key: str):
    assert has_permission([system_role("Editor")], key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    sorted(set(ALL_KEYS) - ROLE_PERMISSIONS["Editor"]),
)
def test_editor_denies(key: str):
    assert not has_permission([system_role("Editor")], key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    sorted(ROLE_PERMISSIONS["Viewer"]),
)
def test_viewer_grants(key: str):
    assert has_permission([system_role("Viewer")], key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    sorted(set(ALL_KEYS) - ROLE_PERMISSIONS["Viewer"]),
)
def test_viewer_denies(key: str):
    assert not has_permission([system_role("Viewer")], key)


# ─── get_user_permissions ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_user_permissions_admin_returns_all_twelve():
    assert get_user_permissions([system_role("Admin")]) == set(ALL_KEYS)


@pytest.mark.unit
def test_get_user_permissions_no_roles_returns_empty_set():
    assert get_user_permissions([]) == set()
    assert get_user_permissions([]) is not None


@pytest.mark.unit
def test_get_user_permissions_multiple_roles_is_union():
    viewer = system_role("Viewer")
    custom = make_role("Auditor", {"analytics:read", "settings:manage"})

    permissions = get_user_permissions([viewer, custom])

    assert permissions == {"document:read", "chat:create", "analytics:read", "settings:manage"}


@pytest.mark.unit
def test_has_permission_no_roles_always_false():
    for key in ALL_KEYS:
        assert not has_permission([], key)


@pytest.mark.unit
def test_has_permission_multiple_roles_grants_from_either():
    viewer = system_role("Viewer")
    editor = system_role("Editor")

    # document:update comes from Editor, not Viewer
    assert has_permission([viewer, editor], "document:update")
    # Viewer alone would deny it
    assert not has_permission([viewer], "document:update")


@pytest.mark.unit
def test_has_permission_accepts_enum_member():
    assert has_permission([system_role("Viewer")], PermissionKey.DOCUMENT_READ)
    assert not has_permission([system_role("Viewer")], PermissionKey.USER_MANAGE)


@pytest.mark.unit
def test_has_permission_unknown_key_returns_false():
    assert not has_permission([system_role("Admin")], "does:not-exist")
