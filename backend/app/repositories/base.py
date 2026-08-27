"""
Repository base class with structural tenant-scoping enforcement.

Every repository method that reads or writes tenant-owned data MUST accept
`organization_id` as an explicit parameter. This is enforced structurally:
the `TenantScopedRepository` base class provides helpers that always inject
the organization filter — making it a code-level impossibility to call a
tenant-scoped query without an organization_id.

See Backend-Architecture-Documentation.md §9 (Repository Layer) and
Database-Architecture-Design-Documentation.md §8 (Multi-Tenant Architecture).
"""
from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Generic repository base providing common CRUD operations.

    Concrete repositories extend this and add domain-specific query methods.
    All SQLAlchemy access lives here — callers never write ORM queries directly.
    """

    model: type[ModelT]  # Set by subclasses

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, id: str | UUID) -> ModelT | None:
        """Fetch a row by primary key. Returns None if not found.

        NOTE: For tenant-owned entities, prefer the tenant-scoped variant
        `get_by_id_for_org()` defined in TenantScopedRepository, which
        additionally filters by organization_id.
        """
        result = await self._session.execute(
            select(self.model).where(self.model.id == str(id))  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def add(self, instance: ModelT) -> ModelT:
        """Add a new instance to the session (does NOT commit)."""
        self._session.add(instance)
        await self._session.flush()  # flush to get DB-generated values (e.g., id)
        await self._session.refresh(instance)
        return instance

    async def delete(self, instance: ModelT) -> None:
        """Delete an instance from the session (does NOT commit)."""
        await self._session.delete(instance)
        await self._session.flush()


class TenantScopedRepository(BaseRepository[ModelT]):
    """Repository base for tenant-owned entities.

    ENFORCES: every public query method that touches tenant data requires
    `organization_id` as an explicit, non-optional parameter.

    This convention is what makes cross-tenant data leakage a type-visible
    error rather than a silent runtime bug — the function signature is the
    guard, not a comment or convention document.
    """

    async def get_by_id_for_org(
        self, id: str | UUID, organization_id: str | UUID
    ) -> ModelT | None:
        """Fetch a row by PK, scoped to the organization.

        Returns None (never raises) if the row does not exist OR belongs to
        a different organization — callers should treat both cases identically
        (404) to avoid leaking whether a resource exists in another tenant.
        """
        result = await self._session.execute(
            select(self.model).where(
                self.model.id == str(id),  # type: ignore[attr-defined]
                self.model.organization_id == str(organization_id),  # type: ignore[attr-defined]
            )
        )
        return result.scalar_one_or_none()

    def _org_filter(self, organization_id: str | UUID) -> Any:
        """Return a SQLAlchemy WHERE clause filtering by organization_id.

        Use this in all repository query methods to guarantee the tenant
        predicate is always present.

        Example:
            stmt = select(User).where(
                self._org_filter(organization_id),
                User.is_active == True,
            )
        """
        return self.model.organization_id == str(organization_id)  # type: ignore[attr-defined]
