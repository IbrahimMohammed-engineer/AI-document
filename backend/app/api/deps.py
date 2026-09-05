"""
FastAPI dependency factory functions.

These are the building blocks for route-level dependency injection:
  - get_db_session: yields a request-scoped async DB session
  - get_current_user: decodes the JWT and loads the authenticated user
  - require_permission: factory that returns a dependency enforcing a
    specific permission key

Phase 0/1: get_db_session is fully functional. get_current_user and
require_permission are stub implementations that will be completed in Phase 2
when AuthService and UserRepository are wired up.
"""
from __future__ import annotations

import logging
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.infrastructure.redis import get_redis
from app.infrastructure.database import get_db_session
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)

# ─── Database session dependency ──────────────────────────────────────────────

# Re-export for convenience — routers import from here, not from infrastructure
DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# ─── Authentication dependencies (Phase 2) ────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbSession,
) -> User:
    """Resolve the authenticated user from the JWT access token.

    Decodes and verifies the bearer token, then re-loads the User (with
    roles + organization) from the database — JWT claims are advisory only.

    Returns:
        The authenticated, active User with roles eager-loaded.

    Raises:
        AuthenticationError: if no token is provided or the user no longer exists.
        TokenExpiredError: if the access token has expired.
        TokenInvalidError: if the token is malformed or has an invalid signature.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    return await AuthService.get_current_user(token=credentials.credentials, db=db)


# Convenience alias — resolves the authenticated user from the JWT access token
CurrentUser = Annotated[User, Depends(get_current_user)]


def require_permission(permission_key: str):
    """FastAPI dependency factory that enforces a specific permission.

    Usage in a router:
        @router.delete("/documents/{id}")
        async def delete_document(
            user = Depends(require_permission("document:delete")),
        ):
            ...

    Live permission enforcement — re-queries the user's roles from the
    database (never trusts JWT claims) and raises InsufficientPermissionsError
    if the key is not granted by any assigned role.

    Args:
        permission_key: One of the system permission keys (e.g., "document:create").

    Returns:
        A FastAPI dependency that resolves to the current user with the
        given permission, or raises InsufficientPermissionsError.
    """
    async def _dependency(
        user: Annotated[User, Depends(get_current_user)],
        db: DbSession,
    ) -> User:
        return await AuthorizationService.check_permission(
            user=user,
            permission_key=permission_key,
            db=db,
        )

    return _dependency


def get_redis_client():
    return get_redis()


# ─── AI endpoint rate limiting (Phase 16) ─────────────────────────────────────

async def check_ai_rate_limit(
    user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],  # type: ignore[type-arg]
) -> None:
    """Per-user sliding-window rate limit for LLM-cost endpoints.

    Wired as a decorator-level ``Depends`` on POST /ask, POST
    /chat/conversations, and POST /chat/conversations/{id}/messages so the
    429 (with Retry-After) is returned BEFORE any retrieval/generation work —
    denial-of-wallet defense (Phase 16 plan §5). Must be re-evaluated per
    request: never cached.
    """
    from app.infrastructure.rate_limiter import check_and_increment

    settings = get_settings()
    key = f"ai_rl:{user.organization_id}:{user.id}"
    await check_and_increment(
        redis,
        key=key,
        limit=settings.ai_rate_limit_requests,
        window_seconds=settings.ai_rate_limit_window_seconds,
        message="Too many AI requests. Please try again later.",
    )
