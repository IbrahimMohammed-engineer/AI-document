"""
Document permission management API (Phase 16 — RESTRICTED isolation matrix).

Endpoints (mounted under the /documents prefix):
  POST   /documents/{document_id}/permissions            — grant explicit access
  GET    /documents/{document_id}/permissions            — list grants
  DELETE /documents/{document_id}/permissions/{user_id}  — revoke a grant

Authorization model (Phase 16 plan §6.2):
  - The document OWNER may always manage grants (bypass).
  - Any other caller needs the ``document:admin`` permission (live DB
    re-check via AuthorizationService.check_permission — never trusted from
    JWT claims).
  - Everything is org-scoped: a document from another organization is a
    hard 404 (existence-leak prevention — never 403).

Audit:
  DOCUMENT_PERMISSION_GRANTED / DOCUMENT_PERMISSION_REVOKED with metadata
  {"grantee_user_id", "permission_type"} — no document content, per the
  Backend §54 no-provider-payload invariant.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession, get_current_user
from app.core.exceptions import ConflictError, NotFoundError
from app.domain.permissions import PermissionKey
from app.models.document import Document, DocumentPermission
from app.models.user import User
from app.schemas.permissions import (
    GrantCreateRequest,
    PermissionGrantItem,
    PermissionListResponse,
)
from app.services.audit_logger import AuditAction, AuditLogger
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["permissions"])


# ── Authorization helper ──────────────────────────────────────────────────────

async def _load_document_for_admin(
    document_id: str, user: User, db: AsyncSession
) -> Document:
    """Load an org-scoped document and assert the caller may manage grants.

    Owner bypass OR the ``document:admin`` permission; cross-org documents
    raise NotFoundError (never 403 — no existence disclosure).
    """
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == user.organization_id,
            Document.deleted_at.is_(None),
        )
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document not found.")
    if document.owner_id != user.id:
        await AuthorizationService.check_permission(
            user=user,
            permission_key=PermissionKey.DOCUMENT_ADMIN,
            db=db,
        )
    return document


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/{document_id}/permissions",
    status_code=201,
    response_model=PermissionGrantItem,
    summary="Grant a user explicit access to a document (RESTRICTED matrix)",
)
async def grant_permission(
    document_id: str,
    body: GrantCreateRequest,
    db: DbSession,
    user: User = Depends(get_current_user),
) -> PermissionGrantItem:
    document = await _load_document_for_admin(document_id, user, db)

    # Grantee must exist in the SAME organization — cross-tenant grants are
    # structurally impossible (the grant row carries the caller's org).
    grantee = (
        await db.execute(
            select(User).where(
                User.id == body.user_id,
                User.organization_id == user.organization_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if grantee is None:
        raise NotFoundError("Grantee user not found.")

    existing = (
        await db.execute(
            select(DocumentPermission).where(
                DocumentPermission.document_id == document.id,
                DocumentPermission.user_id == grantee.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("User already has an explicit grant on this document.")

    grant = DocumentPermission(
        organization_id=user.organization_id,
        document_id=document.id,
        user_id=grantee.id,
        permission_type=body.permission_type,
        granted_by=user.id,
        expires_at=body.expires_at,
    )
    db.add(grant)
    await AuditLogger.log(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        action=AuditAction.DOCUMENT_PERMISSION_GRANTED,
        resource_type="document",
        resource_id=document.id,
        metadata={
            "grantee_user_id": grantee.id,
            "permission_type": body.permission_type,
        },
    )
    await db.commit()
    await db.refresh(grant)

    return PermissionGrantItem(
        id=grant.id,
        user_id=grant.user_id,
        grantee_email=grantee.email,
        grantee_full_name=grantee.full_name,
        permission_type=grant.permission_type,
        expires_at=grant.expires_at,
        created_at=grant.created_at,
    )


@router.get(
    "/{document_id}/permissions",
    response_model=PermissionListResponse,
    summary="List explicit access grants on a document (owner or document:admin)",
)
async def list_permissions(
    document_id: str,
    db: DbSession,
    user: User = Depends(get_current_user),
) -> PermissionListResponse:
    document = await _load_document_for_admin(document_id, user, db)

    result = await db.execute(
        select(DocumentPermission, User)
        .join(User, User.id == DocumentPermission.user_id)
        .where(DocumentPermission.document_id == document.id)
        .order_by(DocumentPermission.created_at)
    )
    items = [
        PermissionGrantItem(
            id=grant.id,
            user_id=grant.user_id,
            grantee_email=grantee.email,
            grantee_full_name=grantee.full_name,
            permission_type=grant.permission_type,
            expires_at=grant.expires_at,
            created_at=grant.created_at,
        )
        for grant, grantee in result.all()
    ]
    return PermissionListResponse(document_id=document.id, items=items)


@router.delete(
    "/{document_id}/permissions/{user_id}",
    status_code=204,
    response_model=None,
    summary="Revoke a user's explicit access grant",
)
async def revoke_permission(
    document_id: str,
    user_id: str,
    db: DbSession,
    user: User = Depends(get_current_user),
) -> Response:
    document = await _load_document_for_admin(document_id, user, db)

    grant = (
        await db.execute(
            select(DocumentPermission).where(
                DocumentPermission.document_id == document.id,
                DocumentPermission.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise NotFoundError("Permission grant not found.")

    await db.delete(grant)
    await AuditLogger.log(
        db,
        organization_id=user.organization_id,
        user_id=user.id,
        action=AuditAction.DOCUMENT_PERMISSION_REVOKED,
        resource_type="document",
        resource_id=document.id,
        metadata={
            "grantee_user_id": user_id,
            "permission_type": grant.permission_type,
        },
    )
    await db.commit()
    return Response(status_code=204)
