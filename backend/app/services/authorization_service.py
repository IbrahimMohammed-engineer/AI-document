"""
Authorization service for live permission checks.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientPermissionsError
from app.domain.permissions import get_user_permissions, has_permission
from app.models.user import User
from app.repositories.user_repository import UserRepository


class AuthorizationService:
    @staticmethod
    async def check_permission(
        *,
        user: User,
        permission_key: str,
        db: AsyncSession,
    ) -> User:
        fresh_user = await UserRepository(db).get_with_roles(user.id, user.organization_id)
        if fresh_user is None:
            raise InsufficientPermissionsError("You no longer have access to this resource.")
        if not has_permission(fresh_user.roles, permission_key):
            raise InsufficientPermissionsError("You do not have permission to perform this action.")
        return fresh_user

    @staticmethod
    def resolve_allowed_document_filter(user: User) -> dict[str, set[str]]:
        return {"permissions": get_user_permissions(user.roles)}
