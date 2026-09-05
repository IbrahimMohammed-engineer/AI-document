"""
Structured-information extraction API router (Phase 14).

Endpoints:
  POST /extractions                          — create a new run (always new;
                                               audit trail, plan §2.6 point 3)
  GET  /extractions/{extraction_id}          — run status + items (COMPLETED)
  GET  /documents/{document_id}/extractions  — run history (most recent
                                               first) — an addition beyond
                                               the roadmap's literal
                                               two-endpoint list, justified in
                                               plan §5.13: extraction results
                                               must be linkable/auditable, and
                                               a history is meaningless
                                               without a list endpoint.

Authorization (plan §5.15): POST requires ``extraction:create``; reads
require ``document:read``; every read re-authorizes the underlying
document/version (404, never 403 — no existence leakage).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.13, §13.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, require_permission
from app.repositories.document_extraction_repository import (
    DocumentExtractionRepository,
)
from app.schemas.extraction import (
    ExtractionCreateRequest,
    ExtractionItemResponse,
    ExtractionItemsByCategory,
    ExtractionRunDetailResponse,
    ExtractionRunResponse,
    ExtractionRunsResponse,
)
from app.services.extraction_service import ExtractionService

router = APIRouter(
    prefix="/extractions",
    tags=["extractions"],
)

documents_router = APIRouter(
    prefix="/documents",
    tags=["extractions"],
)

logger = logging.getLogger(__name__)


# ── POST /extractions ─────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=ExtractionRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run structured-information extraction on a document version",
    description=(
        "Creates a NEW extraction run (every explicit call is a new "
        "audit-trail entry — results are persisted per run and never "
        "overwritten) and enqueues a STRUCTURED_EXTRACTION job.  The caller "
        "polls GET /extractions/{id} for completion.\n\n"
        "**Business rules:**\n"
        "- Requires the `extraction:create` permission (Admin/Editor).\n"
        "- The target version must be READY.\n"
        "- `schema_key` is a closed enum (standard_v1 in V1) — bound anyway, "
        "never free text.\n"
        "- Audit-logged as EXTRACTION_RUN_CREATED."
    ),
)
async def create_extraction_run(
    body: ExtractionCreateRequest,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("extraction:create"))],
) -> ExtractionRunResponse:
    version_id = await ExtractionService.resolve_run_version(
        user,  # type: ignore[arg-type]
        body.document_id,
        body.version,
        db,
    )
    extraction = await ExtractionService.create_run(
        user=user,  # type: ignore[arg-type]
        document_version_id=version_id,
        schema_key=body.schema_key,
        db=db,
    )
    logger.info(
        "POST /extractions: run=%s document=%s user=%s",
        extraction.id, body.document_id, user.id,  # type: ignore[attr-defined]
    )
    return ExtractionRunResponse.model_validate(extraction)


# ── GET /extractions/{extraction_id} ──────────────────────────────────────────

@router.get(
    "/{extraction_id}",
    response_model=ExtractionRunDetailResponse,
    summary="Get one extraction run (items included when COMPLETED)",
    description=(
        "Returns the run's status and, when COMPLETED, its extracted items "
        "grouped by category — each item citation-backed with full page/"
        "section provenance."
    ),
)
async def get_extraction_run(
    extraction_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
) -> ExtractionRunDetailResponse:
    extraction = await ExtractionService.get_run(
        user=user,  # type: ignore[arg-type]
        extraction_id=extraction_id,
        db=db,
    )
    response = ExtractionRunDetailResponse.model_validate(extraction)
    if extraction.status != "COMPLETED":  # type: ignore[attr-defined]
        return response

    repo = DocumentExtractionRepository(db)
    items = await repo.list_items(extraction_id)
    response.items = _group_items(items)
    return response


# ── GET /documents/{document_id}/extractions ──────────────────────────────────

@documents_router.get(
    "/{document_id}/extractions",
    response_model=ExtractionRunsResponse,
    summary="List extraction runs for a document (most recent first)",
    description=(
        "Run history for the Document Workspace's auditable extraction list. "
        "Pagination: `limit` + `offset`."
    ),
)
async def list_extraction_runs(
    document_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ExtractionRunsResponse:
    runs, total = await ExtractionService.list_runs_for_document(
        user=user,  # type: ignore[arg-type]
        document_id=document_id,
        db=db,
        limit=limit,
        offset=offset,
    )
    return ExtractionRunsResponse(
        document_id=document_id,
        total=total,
        items=[ExtractionRunResponse.model_validate(run) for run in runs],
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _group_items(items: list) -> ExtractionItemsByCategory:
    """Group item rows into the fixed four-category payload shape."""
    grouped = ExtractionItemsByCategory()
    for item in items:
        target = getattr(grouped, item.category, None)
        if target is None:
            continue  # defensive: outside the closed category set
        target.append(ExtractionItemResponse.model_validate(item))
    return grouped
