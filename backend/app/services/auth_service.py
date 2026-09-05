"""
Authentication service for registration, login, refresh, logout, and reset flows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, ConflictError, TokenInvalidError
from app.core.security import (
    AlgorithmDowngradeBlockedError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.domain.permissions import get_user_permissions
from app.infrastructure.password_reset_store import consume as consume_password_reset
from app.infrastructure.password_reset_store import store as store_password_reset
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import OrganizationRepository, UserRepository
from app.services.audit_logger import AuditAction, AuditLogger

_DUMMY_PASSWORD_HASH = hash_password("phase-2-dummy-password")

logger = logging.getLogger(__name__)


@dataclass
class AuthResult:
    user: User
    access_token: str
    refresh_token: str
    expires_in: int
    permissions: list[str]


class AuthService:
    @staticmethod
    async def register(
        *,
        org_name: str,
        slug: str,
        email: str,
        full_name: str,
        password: str,
        db: AsyncSession,
        redis: aioredis.Redis,  # type: ignore[type-arg]
        request: Request | None = None,
    ) -> AuthResult:
        del redis
        org_repo = OrganizationRepository(db)
        user_repo = UserRepository(db)

        existing_org = await org_repo.get_by_slug(slug)
        if existing_org is not None:
            raise ConflictError("An organization with this slug already exists.")

        organization = await org_repo.create(name=org_name, slug=slug)
        user = await user_repo.create(
            organization_id=organization.id,
            email=email,
            full_name=full_name,
            password_hash=hash_password(password),
        )

        admin_role = await db.scalar(
            select(Role).where(Role.name == "Admin", Role.organization_id.is_(None))
        )
        if admin_role is None:
            raise ConflictError("System roles are not available.")

        db.add(UserRole(user_id=user.id, role_id=admin_role.id, organization_id=organization.id))
        await db.flush()
        await AuditLogger.log(
            db,
            organization_id=organization.id,
            user_id=user.id,
            action=AuditAction.USER_CREATED,
            resource_type="user",
            resource_id=user.id,
            metadata={"email": user.email, "bootstrap_admin": True},
            request=request,
        )
        await db.commit()

        return await AuthService._issue_token_pair(
            user_id=user.id,
            organization_id=organization.id,
            db=db,
            request=request,
        )

    @staticmethod
    async def login(
        *,
        email: str,
        password: str,
        org_slug: str,
        db: AsyncSession,
        redis: aioredis.Redis,  # type: ignore[type-arg]
        request: Request | None = None,
    ) -> AuthResult:
        del redis
        org = await OrganizationRepository(db).get_by_slug(org_slug)
        user = None if org is None else await UserRepository(db).get_by_email_for_org(email, org.id)

        password_hash = user.password_hash if user and user.password_hash else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(password, password_hash)

        if org is None or user is None or not password_ok:
            if org is not None:
                await AuditLogger.log(
                    db,
                    organization_id=org.id,
                    user_id=user.id if user else None,
                    action=AuditAction.LOGIN_FAILED,
                    resource_type="user",
                    resource_id=user.id if user else None,
                    metadata={"email": email.lower()},
                    request=request,
                )
                await db.commit()
            raise AuthenticationError()

        await UserRepository(db).update_last_login(user.id)
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.USER_LOGIN,
            resource_type="user",
            resource_id=user.id,
            metadata={"email": user.email},
            request=request,
        )
        await db.commit()

        return await AuthService._issue_token_pair(
            user_id=user.id,
            organization_id=user.organization_id,
            db=db,
            request=request,
        )

    @staticmethod
    async def refresh_tokens(
        *,
        raw_refresh_token: str,
        db: AsyncSession,
        redis: aioredis.Redis,  # type: ignore[type-arg]
        request: Request | None = None,
    ) -> AuthResult:
        del redis
        token_repo = RefreshTokenRepository(db)
        token_hash = hash_token(raw_refresh_token)
        token_record = await token_repo.get_by_token_hash(token_hash)

        if token_record is None:
            raise AuthenticationError()

        now = datetime.now(tz=timezone.utc)
        if token_record.revoked_at is not None:
            await token_repo.revoke_all_for_user(token_record.user_id)
            user = await UserRepository(db).get_with_roles(token_record.user_id, token_record.user.organization_id)
            if user is not None:
                await AuditLogger.log(
                    db,
                    organization_id=user.organization_id,
                    user_id=user.id,
                    action=AuditAction.TOKEN_REVOKED_REUSE_DETECTED,
                    resource_type="user",
                    resource_id=user.id,
                    metadata={"refresh_token_id": token_record.id},
                    request=request,
                )
            await db.commit()
            raise AuthenticationError()

        if token_record.expires_at <= now:
            await token_repo.revoke(token_record.id)
            await db.commit()
            raise AuthenticationError()

        await token_repo.revoke(token_record.id)
        await db.commit()

        return await AuthService._issue_token_pair(
            user_id=token_record.user_id,
            organization_id=token_record.user.organization_id,
            db=db,
            request=request,
        )

    @staticmethod
    async def logout(
        *,
        raw_refresh_token: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> None:
        token_repo = RefreshTokenRepository(db)
        token_record = await token_repo.get_by_token_hash(hash_token(raw_refresh_token))
        if token_record is None:
            return
        await token_repo.revoke(token_record.id)
        await AuditLogger.log(
            db,
            organization_id=token_record.user.organization_id,
            user_id=token_record.user_id,
            action=AuditAction.LOGOUT,
            resource_type="user",
            resource_id=token_record.user_id,
            metadata={"refresh_token_id": token_record.id},
            request=request,
        )
        await db.commit()

    @staticmethod
    async def get_current_user(*, token: str, db: AsyncSession) -> User:
        try:
            payload = decode_access_token(token)
        except AlgorithmDowngradeBlockedError:
            # Phase 16: algorithm-confusion attempt (HS256 token presented
            # against RS256-pinned verification) — audit BEFORE the generic
            # 401. The audit row is attributed to the CLAIMED org only when
            # that organization actually exists (the token is attacker-
            # controlled); metadata never carries the token or payload text.
            logger.warning("JWT algorithm downgrade blocked (attempted HS256).")
            claimed_org = None
            try:
                import jwt as _jwt

                claimed_org = _jwt.decode(
                    token, options={"verify_signature": False}
                ).get("org_id")
            except Exception:  # noqa: BLE001 — unparseable tokens have no claim
                claimed_org = None
            if claimed_org:
                try:
                    org_exists = (
                        await db.execute(
                            select(Organization.id).where(
                                Organization.id == claimed_org
                            )
                        )
                    ).scalar_one_or_none()
                    if org_exists:
                        await AuditLogger.log(
                            db,
                            organization_id=claimed_org,
                            user_id=None,
                            action=AuditAction.JWT_ALGORITHM_DOWNGRADE_BLOCKED,
                            resource_type="auth",
                            resource_id=None,
                            metadata={"attempted_algorithm": "HS256"},
                        )
                        await db.commit()
                except Exception:  # noqa: BLE001 — audit must never mask the 401
                    logger.exception(
                        "JWT_ALGORITHM_DOWNGRADE_BLOCKED audit write failed"
                    )
                    await db.rollback()
            raise TokenInvalidError("Access token is invalid.")
        user_id = payload.get("sub")
        org_id = payload.get("org_id")
        if not user_id or not org_id:
            raise TokenInvalidError("Access token is invalid.")

        user = await UserRepository(db).get_with_roles(user_id, org_id)
        if user is None:
            raise AuthenticationError()
        return user

    @staticmethod
    async def request_password_reset(
        *,
        email: str,
        org_slug: str,
        db: AsyncSession,
        redis: aioredis.Redis,  # type: ignore[type-arg]
        request: Request | None = None,
    ) -> str | None:
        org = await OrganizationRepository(db).get_by_slug(org_slug)
        if org is None:
            return None

        user = await UserRepository(db).get_by_email_for_org(email, org.id)
        if user is None:
            return None

        raw_token = create_refresh_token()
        await store_password_reset(
            redis,
            token_hash=hash_token(raw_token),
            payload={"user_id": user.id, "org_id": user.organization_id},
        )
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.PASSWORD_RESET_REQUESTED,
            resource_type="user",
            resource_id=user.id,
            metadata={"email": user.email},
            request=request,
        )
        await db.commit()
        return raw_token

    @staticmethod
    async def reset_password(
        *,
        reset_token: str,
        new_password: str,
        db: AsyncSession,
        redis: aioredis.Redis,  # type: ignore[type-arg]
        request: Request | None = None,
    ) -> None:
        payload = await consume_password_reset(redis, token_hash=hash_token(reset_token))
        if payload is None:
            raise AuthenticationError("Reset token is invalid or has expired.")

        user_id = payload["user_id"]
        org_id = payload["org_id"]
        user_repo = UserRepository(db)
        user = await user_repo.get_with_roles(user_id, org_id)
        if user is None:
            raise AuthenticationError("Reset token is invalid or has expired.")

        await user_repo.update_password_hash(user.id, user.organization_id, hash_password(new_password))
        await RefreshTokenRepository(db).revoke_all_for_user(user.id)
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.PASSWORD_RESET_COMPLETED,
            resource_type="user",
            resource_id=user.id,
            metadata={},
            request=request,
        )
        await db.commit()

    @staticmethod
    async def _issue_token_pair(
        *,
        user_id: str,
        organization_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> AuthResult:
        user = await UserRepository(db).get_with_roles(user_id, organization_id)
        if user is None:
            raise AuthenticationError()

        permissions = sorted(get_user_permissions(user.roles))
        access_token = create_access_token(
            user.id,
            org_id=user.organization_id,
            extra_claims={"roles": [role.name for role in user.roles]},
        )
        raw_refresh_token = create_refresh_token()
        refresh_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            days=get_settings().jwt_refresh_token_expire_days
        )
        await RefreshTokenRepository(db).create(
            user_id=user.id,
            token_hash=hash_token(raw_refresh_token),
            expires_at=refresh_expires_at,
            user_agent=request.headers.get("user-agent") if request else None,
            ip_address=request.client.host if request and request.client else None,
        )
        await db.commit()
        return AuthResult(
            user=user,
            access_token=access_token,
            refresh_token=raw_refresh_token,
            expires_in=get_settings().jwt_access_token_expire_minutes * 60,
            permissions=permissions,
        )
