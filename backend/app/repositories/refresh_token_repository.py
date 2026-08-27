"""
Repositories for refresh tokens and audit log writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from app.models.user import AuditLog, RefreshToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def create(
        self,
        *,
        user_id: str | UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=str(user_id),
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return await self.add(token)

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken)
            .options(selectinload(RefreshToken.user))
            .where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke(self, token_id: str | UUID) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == str(token_id), RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(tz=timezone.utc))
        )
        await self._session.flush()

    async def revoke_all_for_user(self, user_id: str | UUID) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == str(user_id), RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(tz=timezone.utc))
        )
        await self._session.flush()

    async def delete_expired(self) -> int:
        result = await self._session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < datetime.now(tz=timezone.utc))
        )
        await self._session.flush()
        return int(result.rowcount or 0)


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def create(
        self,
        *,
        organization_id: str | UUID,
        user_id: str | UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | UUID | None,
        metadata: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            organization_id=str(organization_id),
            user_id=str(user_id) if user_id else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            metadata_=metadata or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return await self.add(entry)
