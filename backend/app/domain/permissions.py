"""
Pure permission evaluation helpers.

This module intentionally contains no framework or ORM code so permission
resolution stays fully unit-testable.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from app.models.user import Role


class PermissionKey(StrEnum):
    DOCUMENT_CREATE = "document:create"
    DOCUMENT_READ = "document:read"
    DOCUMENT_UPDATE = "document:update"
    DOCUMENT_DELETE = "document:delete"
    CHAT_CREATE = "chat:create"
    COMPARISON_CREATE = "comparison:create"
    USER_MANAGE = "user:manage"
    SETTINGS_MANAGE = "settings:manage"
    ANALYTICS_READ = "analytics:read"


def get_user_permissions(user_roles: Iterable[Role]) -> set[str]:
    """Return the union of permissions across all assigned roles."""
    permissions: set[str] = set()
    for role in user_roles:
        for permission in role.permissions:
            permissions.add(permission.key)
    return permissions


def has_permission(user_roles: Iterable[Role], key: str | PermissionKey) -> bool:
    """Return True if any assigned role grants the permission key."""
    return str(key) in get_user_permissions(user_roles)
