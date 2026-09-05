"""
User and Organization repositories.

These are the Phase 1 repositories. They implement the repository pattern
with structural tenant-scoping (every tenant-scoped method requires
`organization_id` as an explicit parameter — see repositories/base.py).

Phase 2 will add:
  - UserRepository.get_by_email_for_org  (used by AuthService.login)
  - UserRepository.get_with_roles        (loads roles for permission resolution)
  - RefreshTokenRepository               (for token rotation/revocation)
  - AuditLogRepository                   (for AuditLogger utility)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = logging.getLogger(__name__)


# ─── Organization Repository ──────────────────────────────────────────────────

class OrganizationRepository(BaseRepository[Organization]):
    """Repository for the `organizations` table.

    Organizations are the tenant root — they have no organization_id themselves,
    so this extends the non-scoped BaseRepository.
    """

    model = Organization

    async def get_by_slug(self, slug: str) -> Organization | None:
        """Fetch an organization by its URL slug.

        Used for SSO/subdomain routing at login time, before the user's
        organization_id is otherwise known.
        """
        result = await self._session.execute(
            select(Organization).where(Organization.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, id: str | UUID) -> Organization | None:
        """Fetch an organization by its UUID primary key."""
        return await super().get_by_id(id)

    async def create(
        self,
        *,
        name: str,
        slug: str,
        plan: str = "standard",
    ) -> Organization:
        """Create a new organization.

        Args:
            name: Display name.
            slug: URL-safe identifier (must be globally unique).
            plan: Subscription tier.

        Returns:
            The persisted Organization instance (with id populated).
        """
        org = Organization(name=name, slug=slug, plan=plan)
        return await self.add(org)


# ─── User Repository ──────────────────────────────────────────────────────────

class UserRepository(TenantScopedRepository[User]):
    """Repository for the `users` table.

    All public query methods that return user data require `organization_id`
    as an explicit parameter — enforced by the TenantScopedRepository base.
    """

    model = User

    async def get_by_id_for_org(
        self, id: str | UUID, organization_id: str | UUID
    ) -> User | None:
        """Fetch a user by PK, scoped to the organization.

        Returns None if the user doesn't exist OR belongs to a different org.
        Callers must treat both cases as 404 (never expose whether a user
        exists in another tenant).
        """
        return await super().get_by_id_for_org(id, organization_id)

    async def get_by_email_for_org(
        self, email: str, organization_id: str | UUID
    ) -> User | None:
        """Fetch an active user by email, scoped to the organization.

        Used by AuthService.login. Returns None if the user doesn't exist,
        is soft-deleted, or is inactive — callers use a generic 401 to
        prevent user enumeration.
        """
        result = await self._session.execute(
            select(User).where(
                self._org_filter(organization_id),
                User.email == email.lower(),
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_org(
        self,
        organization_id: str | UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        """Paginated member list for the Settings → Users page (Phase 15).

        Includes inactive users (they are still members shown in the table)
        but excludes soft-deleted ones. Roles are eager-loaded so the API can
        render role badges without N+1 queries.

        Returns:
            (users ordered by created_at then full_name, total matching rows)
        """
        base = select(User).where(
            self._org_filter(organization_id),
            User.deleted_at.is_(None),
        )

        total_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = int(total_result.scalar_one())

        result = await self._session.execute(
            base.options(selectinload(User.roles))
            .order_by(User.created_at, User.full_name)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total

    async def update_profile(
        self,
        user_id: str | UUID,
        *,
        full_name: str,
    ) -> None:
        """Update the caller's own display name (PATCH /settings/profile)."""
        await self._session.execute(
            update(User)
            .where(User.id == str(user_id), User.deleted_at.is_(None))
            .values(full_name=full_name)
        )
        await self._session.flush()

    async def set_roles(
        self,
        user_id: str | UUID,
        organization_id: str | UUID,
        role_ids: list[str | UUID],
    ) -> None:
        """Replace a user's role assignments (org-scoped, Phase 15).

        Deletes existing user_roles rows then inserts the new set. Role IDs
        are validated by the caller (must exist and belong to the org or be
        system roles). Does NOT commit — the request-scoped transaction does.
        """
        await self._session.execute(
            delete(UserRole).where(
                UserRole.user_id == str(user_id),
                UserRole.organization_id == str(organization_id),
            )
        )
        for role_id in role_ids:
            self._session.add(
                UserRole(
                    user_id=str(user_id),
                    role_id=str(role_id),
                    organization_id=str(organization_id),
                )
            )
        await self._session.flush()

    async def list_for_org(
        self,
        organization_id: str | UUID,
        *,
        include_inactive: bool = False,
    ) -> list[User]:
        """List all users in an organization.

        Args:
            organization_id: The tenant's UUID.
            include_inactive: If False (default), exclude soft-deleted and
                inactive users.

        Returns:
            A list of User instances ordered by full_name.
        """
        stmt = select(User).where(self._org_filter(organization_id))
        if not include_inactive:
            stmt = stmt.where(User.deleted_at.is_(None), User.is_active.is_(True))
        stmt = stmt.order_by(User.full_name)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_with_roles(
        self, user_id: str | UUID, organization_id: str | UUID
    ) -> User | None:
        result = await self._session.execute(
            select(User)
            .options(selectinload(User.organization), selectinload(User.roles).selectinload(Role.permissions))
            .where(
                self._org_filter(organization_id),
                User.id == str(user_id),
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def update_last_login(self, user_id: str | UUID) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == str(user_id))
            .values(last_login_at=datetime.now(tz=timezone.utc))
        )
        await self._session.flush()

    async def update_password_hash(
        self,
        user_id: str | UUID,
        organization_id: str | UUID,
        new_hash: str,
    ) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == str(user_id), self._org_filter(organization_id))
            .values(password_hash=new_hash)
        )
        await self._session.flush()

    async def create(
        self,
        *,
        organization_id: str | UUID,
        email: str,
        full_name: str,
        password_hash: str | None = None,
    ) -> User:
        """Create a new user within an organization.

        Args:
            organization_id: The tenant's UUID.
            email: Email address (stored lowercase; must be unique within the org).
            full_name: Display name.
            password_hash: Argon2 hash from security.hash_password(). NULL for SSO users.

        Returns:
            The persisted User instance.
        """
        user = User(
            organization_id=str(organization_id),
            email=email.lower(),
            full_name=full_name,
            password_hash=password_hash,
        )
        return await self.add(user)

    async def soft_delete(self, user: User) -> User:
        """Soft-delete a user (sets deleted_at; does NOT commit).

        Users are soft-deleted on offboarding and are never hard-deleted
        while FK-referencing rows exist.
        """
        from datetime import datetime, timezone

        user.deleted_at = datetime.now(tz=timezone.utc)
        user.is_active = False
        await self._session.flush()
        return user
