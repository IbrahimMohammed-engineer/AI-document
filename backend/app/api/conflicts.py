"""
Conflict detection API router (Phase 13).

Endpoints (the roadmap's literal list — §4.6 Gap 3):
  GET  /conflicts                   — authorized conflict list (status/severity filter)
  GET  /conflicts/scan-status       — background-scan status snapshot
  GET  /conflicts/{conflict_id}     — detail embedding its evidence statements
  POST /conflicts/{conflict_id}/resolve — terminal REVIEWED/DISMISSED transition

Authorization (§18, §27.4 — non-negotiable):
  - List/detail require authentication only; SOURCE-DOCUMENT authorization
    filters the result set.  A conflict is returned only when EVERY
    statement's source document passes the platform's access-level rule
    (private → owner only; organization/restricted → org-readable).  A user
    who cannot see a private-source conflict gets the same 200-without-it /
    404 as anyone else — structurally indistinguishable from "doesn't exist"
    (no existence leakage; always 404, never 403 for existence).
  - POST resolve additionally requires the ``conflict:resolve`` permission
    (Admin/Editor, seeded by migration 013) and re-checks source
    authorization live at resolve time.
  - GET /conflicts/scan-status requires authentication only (org-scoped).

Route ordering note: /conflicts/scan-status is declared BEFORE
/conflicts/{conflict_id} so the static path wins (FastAPI matches in order).
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, DbSession, require_permission
from app.core.exceptions import NotFoundError
from app.schemas.conflict import (
    ConflictDetailResponse,
    ConflictListResponse,
    ConflictResolveRequest,
    ConflictSummary,
    ScanStatusResponse,
)
from app.services.conflict_service import ConflictService

router = APIRouter(
    prefix="/conflicts",
    tags=["conflicts"],
)

logger = logging.getLogger(__name__)


# ── GET /conflicts ────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=ConflictListResponse,
    summary="List conflicts visible to the requesting user",
    description=(
        "Returns the conflicts whose every statement's source document is "
        "readable by the requesting user (private documents are visible to "
        "their owner only).  Defaults to OPEN conflicts; `priority` is "
        "computed live from the current effective-date state of every "
        "statement's version (ACTIVE / LIKELY_RESOLVED).  Returned "
        "unpaginated by design — conflict volume is bounded by the "
        "conservative candidate/confidence thresholds (plan §18 deliberate "
        "simplification)."
    ),
)
async def list_conflicts(
    db: DbSession,
    user: CurrentUser,
    status: Optional[str] = Query(
        "OPEN",
        description="Filter by lifecycle status.",
        pattern="^(OPEN|REVIEWED|DISMISSED)$",
    ),
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: MAJOR, MODERATE, or MINOR.",
        pattern="^(MAJOR|MODERATE|MINOR)$",
    ),
) -> ConflictListResponse:
    items = await ConflictService.list_conflicts(
        organization_id=user.organization_id,
        requesting_user=user,
        status=status,
        severity=severity,
        db=db,
    )
    return ConflictListResponse(
        items=[ConflictSummary(**item) for item in items]
    )


# ── GET /conflicts/scan-status ────────────────────────────────────────────────

@router.get(
    "/scan-status",
    response_model=ScanStatusResponse,
    summary="Background conflict-scan status",
    description=(
        "Status of the org's nightly conflict scan: the latest CONFLICT_SCAN "
        "job's status, when a scan last completed and how many conflicts it "
        "created, and how many active documents have not yet been covered by "
        "a completed scan (drives the UI's partial-coverage banner)."
    ),
)
async def get_scan_status(
    db: DbSession,
    user: CurrentUser,
) -> ScanStatusResponse:
    snapshot = await ConflictService.get_scan_status(
        organization_id=user.organization_id, db=db
    )
    return ScanStatusResponse(**snapshot)


# ── GET /conflicts/{conflict_id} ──────────────────────────────────────────────

@router.get(
    "/{conflict_id}",
    response_model=ConflictDetailResponse,
    summary="Get one conflict with its evidence statements",
    description=(
        "Returns the conflict detail embedding its N evidence statements "
        "(document name, version, page, section, statement text, denormalized "
        "effective_date, and the LIVE version_state).  A conflict backed by "
        "any statement source the user cannot read resolves to 404 — "
        "indistinguishable from a nonexistent conflict (no existence leakage)."
    ),
)
async def get_conflict(
    conflict_id: str,
    db: DbSession,
    user: CurrentUser,
) -> ConflictDetailResponse:
    detail = await ConflictService.get_conflict_detail(
        conflict_id=conflict_id,
        requesting_user=user,
        db=db,
    )
    if detail is None:
        raise NotFoundError("Conflict not found.")
    return ConflictDetailResponse(**detail)


# ── POST /conflicts/{conflict_id}/resolve ─────────────────────────────────────

@router.post(
    "/{conflict_id}/resolve",
    response_model=ConflictDetailResponse,
    summary="Resolve a conflict (terminal REVIEWED / DISMISSED transition)",
    description=(
        "Applies the audited, role-gated terminal transition (OPEN → REVIEWED "
        "or OPEN → DISMISSED).  Requires the `conflict:resolve` permission "
        "(Admin/Editor).  Resolution is never reopened by later scans; a "
        "resolved conflict's exact statement pair is deduplicated forever.  "
        "409 when the conflict is already resolved; 404 when the conflict "
        "does not exist, is cross-org, or any statement's source document is "
        "not readable by the requester."
    ),
)
async def resolve_conflict(
    conflict_id: str,
    body: ConflictResolveRequest,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("conflict:resolve"))],
) -> ConflictDetailResponse:
    await ConflictService.resolve(
        conflict_id,
        user=user,  # type: ignore[arg-type]
        decision=body.decision,
        note=body.note,
        db=db,
    )
    detail = await ConflictService.get_conflict_detail(
        conflict_id=conflict_id,
        requesting_user=user,  # type: ignore[arg-type]
        db=db,
    )
    if detail is None:  # pragma: no cover — resolve just authorized it
        raise NotFoundError("Conflict not found.")
    logger.info(
        "POST /conflicts/%s/resolve: decision=%s user=%s",
        conflict_id, body.decision, user.id,  # type: ignore[attr-defined]
    )
    return ConflictDetailResponse(**detail)
