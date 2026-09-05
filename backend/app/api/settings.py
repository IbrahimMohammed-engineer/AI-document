"""
Settings + audit-log read API (Phase 15).

Endpoints:
  PATCH /settings/profile                    — own display name (no permission)
  PATCH /settings/organization               — org name (settings:manage)
  GET   /settings/users                      — paginated members (user:manage)
  PATCH /settings/users/{user_id}/roles      — replace role set (user:manage)
  GET   /settings/roles                      — roles + permission keys (user:manage)

  GET   /audit-logs                          — filtered audit trail (settings:manage)

Permission mapping note (plan §5.1 uses a shorthand "org:manage"): the seeded
permission catalog (migration 002) splits the equivalent into ``user:manage``
(users + roles) and ``settings:manage`` (organization settings + audit data).
This router uses those existing keys — no new permission rows are needed.

Layering: thin CRUD over UserRepository / AuditLogRepository — no business
logic lives here. Org isolation always comes from the JWT (never a client
parameter).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, require_permission
from app.core.exceptions import AppException, NotFoundError, ValidationError
from app.models.organization import Organization
from app.models.user import Role, User
from app.repositories.refresh_token_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import OrganizationResponse, UserMeResponse
from app.services.audit_logger import AuditAction, AuditLogger

router = APIRouter(prefix="/settings", tags=["settings"])
audit_router = APIRouter(prefix="/audit-logs", tags=["audit"])

logger = logging.getLogger(__name__)


class RoleValidationError(AppException):
    """Semantic request-body error → 422 (plan §5.1.4 / §9.3 test matrix)."""

    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "VALIDATION_ERROR"


# ── Schemas ────────────────────────────────────────────────────────────────────

class UpdateProfileRequest(BaseModel):
    """PATCH /settings/profile body — display name only (email is immutable)."""

    full_name: str = Field(min_length=2, max_length=255)

    @field_validator("full_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("full_name must be at least 2 characters")
        return value


class UpdateOrganizationRequest(BaseModel):
    """PATCH /settings/organization body — name only (slug is immutable)."""

    name: str = Field(min_length=2, max_length=255)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("name must be at least 2 characters")
        return value


class UpdateUserRolesRequest(BaseModel):
    """PATCH /settings/users/{user_id}/roles body — full replacement set."""

    role_ids: list[str] = Field(max_length=20)


class SettingsUserItem(BaseModel):
    """One member row of GET /settings/users."""

    id: str
    email: str
    full_name: str
    roles: list[str]
    role_ids: list[str]
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SettingsUsersResponse(BaseModel):
    items: list[SettingsUserItem]
    total: int
    limit: int
    offset: int


class RoleItem(BaseModel):
    id: str
    name: str
    is_system: bool
    permissions: list[str]


class RolesResponse(BaseModel):
    items: list[RoleItem]
    total: int


class AuditLogItem(BaseModel):
    id: str
    organization_id: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    ip_address: Optional[str] = None
    created_at: datetime


class AuditLogsResponse(BaseModel):
    items: list[AuditLogItem]
    total: int
    limit: int
    offset: int


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _load_me(user: User, db: DbSession) -> UserMeResponse:
    """Rebuild the /auth/me shape from a freshly loaded user."""
    fresh = await UserRepository(db).get_with_roles(user.id, user.organization_id)
    if fresh is None:  # pragma: no cover — user deleted mid-request
        raise NotFoundError("User not found.")
    return UserMeResponse(
        id=fresh.id,
        email=fresh.email,
        full_name=fresh.full_name,
        organization=fresh.organization,
        permissions=sorted(
            {permission.key for role in fresh.roles for permission in role.permissions}
        ),
    )


def _role_item(role: Role, permissions: list[str]) -> RoleItem:
    return RoleItem(
        id=role.id,
        name=role.name,
        is_system=role.is_system,
        permissions=sorted(permissions),
    )


# ── PATCH /settings/profile ────────────────────────────────────────────────────

@router.patch(
    "/profile",
    response_model=UserMeResponse,
    summary="Update the current user's own profile",
    description=(
        "Updates the caller's own display name. Returns the same shape as "
        "GET /auth/me so the client can refresh its session user directly. "
        "Email and organization are immutable here.\n\n"
        "Not audit-logged: profile display-name changes are not "
        "security-relevant (Backend §54)."
    ),
)
async def update_profile(
    payload: UpdateProfileRequest,
    db: DbSession,
    user: CurrentUser,
) -> UserMeResponse:
    await UserRepository(db).update_profile(user.id, full_name=payload.full_name)
    response = await _load_me(user, db)
    await db.commit()
    logger.info("PATCH /settings/profile: user=%s", user.id)
    return response


# ── PATCH /settings/organization ───────────────────────────────────────────────

@router.patch(
    "/organization",
    response_model=OrganizationResponse,
    summary="Update the organization name",
    description=(
        "Requires the `settings:manage` permission (admins). The URL slug is "
        "deliberately immutable — slug changes break org routing and existing "
        "login URLs."
    ),
)
async def update_organization(
    payload: UpdateOrganizationRequest,
    db: DbSession,
    user: Annotated[User, Depends(require_permission("settings:manage"))],
) -> OrganizationResponse:
    repo = UserRepository(db)
    org = await db.get(Organization, user.organization_id)
    if org is None:  # pragma: no cover — org FK always exists
        raise NotFoundError("Organization not found.")
    org.name = payload.name
    await db.flush()
    await db.commit()
    logger.info(
        "PATCH /settings/organization: org=%s by user=%s",
        org.id, user.id,
    )
    return OrganizationResponse.model_validate(org)


# ── GET /settings/users ────────────────────────────────────────────────────────

@router.get(
    "/users",
    response_model=SettingsUsersResponse,
    summary="List organization members",
    description=(
        "Paginated member list for the Settings → Users page. Requires the "
        "`user:manage` permission. Org-scoped from the JWT — never a client "
        "parameter."
    ),
)
async def list_users(
    db: DbSession,
    user: Annotated[User, Depends(require_permission("user:manage"))],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> SettingsUsersResponse:
    users, total = await UserRepository(db).list_by_org(
        user.organization_id, limit=limit, offset=offset
    )
    return SettingsUsersResponse(
        items=[
            SettingsUserItem(
                id=u.id,
                email=u.email,
                full_name=u.full_name,
                roles=[role.name for role in u.roles],
                role_ids=[role.id for role in u.roles],
                is_active=u.is_active,
                created_at=u.created_at,
            )
            for u in users
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── PATCH /settings/users/{user_id}/roles ──────────────────────────────────────

async def _validate_role_ids(
    db: DbSession,
    organization_id: str,
    role_ids: list[str],
) -> list[Role]:
    """Resolve + validate the requested role set for the org.

    Accepts system roles (organization_id IS NULL) and the org's own custom
    roles. Anything else (another org's role, unknown id) is a 404-safe 422.
    """
    if not role_ids:
        raise RoleValidationError("At least one role must be assigned.")
    unique_ids = list(dict.fromkeys(role_ids))
    result = await db.execute(
        select(Role).options(selectinload(Role.permissions)).where(Role.id.in_(unique_ids))
    )
    roles = list(result.scalars().unique().all())
    valid = [
        role
        for role in roles
        if role.organization_id is None or role.organization_id == organization_id
    ]
    if len(valid) != len(unique_ids):
        raise RoleValidationError("One or more role IDs are invalid for this organization.")
    return valid


def _role_grants(roles: list[Role]) -> set[str]:
    return {permission.key for role in roles for permission in role.permissions}


@router.patch(
    "/users/{user_id}/roles",
    response_model=SettingsUserItem,
    summary="Replace a member's role assignments",
    description=(
        "Full-replacement role update (the request body is the complete new "
        "set). Requires `user:manage`. Self-lockout guard: an admin cannot "
        "remove the last role granting `user:manage` from themselves (422). "
        "Audit-logged as PERMISSION_CHANGED."
    ),
)
async def update_user_roles(
    user_id: str,
    payload: UpdateUserRolesRequest,
    request: Request,
    db: DbSession,
    user: Annotated[User, Depends(require_permission("user:manage"))],
) -> SettingsUserItem:
    repo = UserRepository(db)
    target = await repo.get_by_id_for_org(user_id, user.organization_id)
    if target is None:
        # Same response for missing AND cross-org users (no existence leak)
        raise NotFoundError("User not found.")

    roles = await _validate_role_ids(db, user.organization_id, payload.role_ids)

    # Self-lockout guard: modifying your own roles must keep user:manage.
    if target.id == user.id and "user:manage" not in _role_grants(roles):
        raise RoleValidationError(
            "You cannot remove your own administrative access.",
            field="role_ids",
        )

    previous = [role.name for role in target.roles]
    await repo.set_roles(target.id, user.organization_id, [role.id for role in roles])

    # Re-load fresh (populate_existing defeats the identity-map instance that
    # still carries the pre-update empty roles collection).
    reload_result = await db.execute(
        select(User)
        .options(selectinload(User.roles))
        .where(User.id == str(target.id))
        .execution_options(populate_existing=True)
    )
    updated = reload_result.scalar_one()

    await AuditLogger.log(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        action=AuditAction.PERMISSION_CHANGED,
        resource_type="user",
        resource_id=target.id,
        metadata={
            "target_user_id": target.id,
            "previous_roles": previous,
            "new_roles": [role.name for role in roles],
        },
        request=request,
    )
    await db.commit()

    logger.info(
        "PATCH /settings/users/%s/roles: %s -> %s by user=%s",
        target.id, previous, [role.name for role in roles], user.id,
    )
    return SettingsUserItem(
        id=updated.id,
        email=updated.email,
        full_name=updated.full_name,
        roles=[role.name for role in updated.roles],
        role_ids=[role.id for role in updated.roles],
        is_active=updated.is_active,
        created_at=updated.created_at,
    )


# ── GET /settings/roles ────────────────────────────────────────────────────────

@router.get(
    "/roles",
    response_model=RolesResponse,
    summary="List assignable roles with permission keys",
    description=(
        "System roles plus the org's custom roles, each with its granted "
        "permission keys. Read-only view for the Settings → Roles page. "
        "Requires `user:manage`."
    ),
)
async def list_roles(
    db: DbSession,
    user: Annotated[User, Depends(require_permission("user:manage"))],
) -> RolesResponse:
    result = await db.execute(
        select(Role)
        .options(selectinload(Role.permissions))
        .where(
            (Role.organization_id.is_(None))
            | (Role.organization_id == user.organization_id)
        )
        .order_by(Role.is_system.desc(), Role.name)
    )
    roles = list(result.scalars().unique().all())
    return RolesResponse(
        items=[
            _role_item(role, [permission.key for permission in role.permissions])
            for role in roles
        ],
        total=len(roles),
    )


# ── GET /audit-logs ────────────────────────────────────────────────────────────

@audit_router.get(
    "",
    response_model=AuditLogsResponse,
    summary="Read the organization's audit trail",
    description=(
        "Newest-first, filtered audit-log page for the Settings → Audit Log "
        "screen. Requires `settings:manage` (audit data is PII-adjacent). "
        "Org-isolated by the JWT — the org filter is never client-supplied."
    ),
)
async def list_audit_logs(
    db: DbSession,
    user: Annotated[User, Depends(require_permission("settings:manage"))],
    action: Optional[str] = Query(None, description="Exact action, e.g. DOCUMENT_UPLOADED"),
    resource_type: Optional[str] = Query(None, description="e.g. document, user"),
    user_id: Optional[str] = Query(None, description="Filter by acting user"),
    from_: Annotated[
        Optional[datetime],
        Query(alias="from", description="ISO-8601 lower bound (inclusive)"),
    ] = None,
    to: Annotated[
        Optional[datetime],
        Query(description="ISO-8601 upper bound (inclusive)"),
    ] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> AuditLogsResponse:
    if from_ is not None and to is not None and from_ > to:
        raise ValidationError("'from' must be before 'to'.", field="from")

    entries, total = await AuditLogRepository(db).list_by_org(
        user.organization_id,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        date_from=from_,
        date_to=to,
        limit=limit,
        offset=offset,
    )
    return AuditLogsResponse(
        items=[
            AuditLogItem(
                id=entry.id,
                organization_id=entry.organization_id,
                user_id=entry.user_id,
                user_email=email,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                metadata=entry.metadata_ or {},
                ip_address=entry.ip_address,
                created_at=entry.created_at,
            )
            for entry, email in entries
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
