"""
Document summary API router (Phase 14).

Endpoints:
  GET  /summaries/{document_id}?version=   — get-or-create-and-poll in one
                                             endpoint (the roadmap's literal
                                             API list): resolves the target
                                             version, creates the PENDING row
                                             + job when absent, returns the
                                             current row's status/content.
  POST /summaries/{document_id}/regenerate — fresh-job regeneration
                                             (permission-gated).

Authorization (plan §5.15 — non-negotiable):
  - GET requires ``document:read``; POST requires ``summary:regenerate``.
  - EVERY read re-runs ``AuthorizationService.authorize_document_version``
    (inside the service) so a later ``access_level`` change on the document
    is respected on every subsequent read — 404, never 403 (no existence
    leakage).

Status-code convention mirrors compare.py exactly: 202 Accepted when a new
job was just created; 200 OK otherwise.

See PHASE-14-IMPLEMENTATION-PLAN.md §5.13, §13.
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import DbSession, require_permission
from app.schemas.summary import SummaryRegenerateRequest, SummaryResponse
from app.services.summary_service import SummaryService

router = APIRouter(
    prefix="/summaries",
    tags=["summaries"],
)

logger = logging.getLogger(__name__)


@router.get(
    "/{document_id}",
    response_model=SummaryResponse,
    summary="Get or create the summary for a document version",
    description=(
        "Returns the current summary status/content for the document's "
        "current (or explicitly requested) version, creating and enqueuing a "
        "SUMMARY job on first access.  The caller polls this endpoint until "
        "status is COMPLETED.\n\n"
        "**Business rules:**\n"
        "- Requires `document:read`.\n"
        "- The target version must be READY.\n"
        "- Idempotent: an existing row is reused regardless of status.\n"
        "- `202 Accepted` when a new generation job was just created; "
        "`200 OK` otherwise."
    ),
)
async def get_summary(
    document_id: str,
    response: Response,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
    version: Optional[int] = Query(
        None,
        description="Version number; null resolves the document's current version.",
        ge=1,
    ),
) -> SummaryResponse:
    version_id = await SummaryService.resolve_summary_version(
        user,  # type: ignore[arg-type]
        document_id,
        version,
        db,
    )
    summary, created = await SummaryService.get_or_create_summary(
        user=user,  # type: ignore[arg-type]
        document_version_id=version_id,
        db=db,
    )
    response.status_code = (
        status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    )
    logger.info(
        "GET /summaries/%s: summary=%s created=%s user=%s",
        document_id, summary.id, created, user.id,  # type: ignore[attr-defined]
    )
    return SummaryResponse.model_validate(summary)


@router.post(
    "/{document_id}/regenerate",
    response_model=SummaryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Regenerate a document version's summary",
    description=(
        "Always creates a fresh SUMMARY job and resets the row to PENDING.  "
        "The existing summary content stays visible (with a stale/"
        "regenerating warning) until the new result completes and overwrites "
        "it.\n\n**Business rules:**\n"
        "- Requires the `summary:regenerate` permission (Admin/Editor).\n"
        "- The target version must be READY.\n"
        "- Audit-logged as SUMMARY_REGENERATED."
    ),
)
async def regenerate_summary(
    document_id: str,
    body: SummaryRegenerateRequest,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("summary:regenerate"))],
) -> SummaryResponse:
    version_id = await SummaryService.resolve_summary_version(
        user,  # type: ignore[arg-type]
        document_id,
        body.version,
        db,
    )
    summary = await SummaryService.regenerate_summary(
        user=user,  # type: ignore[arg-type]
        document_version_id=version_id,
        db=db,
    )
    logger.info(
        "POST /summaries/%s/regenerate: summary=%s user=%s",
        document_id, summary.id, user.id,  # type: ignore[attr-defined]
    )
    return SummaryResponse.model_validate(summary)
