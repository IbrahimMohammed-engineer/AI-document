"""
Document comparison API router (Phase 12).

Endpoints:
  POST  /documents/compare                             — initiate or reuse a comparison
  GET   /documents/compare/{comparison_id}             — comparison status + summary
  GET   /documents/compare/{comparison_id}/changes     — list of changes (+ filters)
  GET   /documents/compare/{comparison_id}/changes/{change_id} — one change + sources
  GET   /documents/compare/{comparison_id}/narration   — LLM-generated narrative

Authorization (plan §13 — non-negotiable):
  - POST requires the ``comparison:create`` permission (PermissionKey
    COMPARISON_CREATE — seeded to Admin/Editor by migration 002).
  - EVERY read re-runs ``AuthorizationService.authorize_document_version``
    for BOTH source versions before returning anything.  A comparison row
    can outlive a later ``access_level`` change on either document, and a
    failure on either side is surfaced as 404 (never 403) so resource
    existence is never leaked through a differentiated status code.

See PHASE-12-IMPLEMENTATION-PLAN.md §9.2, §13.
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, require_permission
from app.core.exceptions import NotFoundError
from app.repositories.document_comparison_repository import DocumentComparisonRepository
from app.schemas.comparison import (
    CompareRequest,
    ComparisonChangeResponse,
    ComparisonChangesResponse,
    ComparisonCreateResponse,
    ComparisonNarrationResponse,
    ComparisonResponse,
    ComparisonSummary,
    SourceRef,
)
from app.services.authorization_service import AuthorizationService
from app.services.comparison_service import ComparisonService

router = APIRouter(
    prefix="/documents",
    tags=["comparisons"],
)

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_summary(comparison: object) -> Optional[ComparisonSummary]:
    raw_summary = getattr(comparison, "summary", None)
    if not isinstance(raw_summary, dict):
        return None
    return ComparisonSummary(
        total=raw_summary.get("total", 0),
        major=raw_summary.get("major", 0),
        moderate=raw_summary.get("moderate", 0),
        minor=raw_summary.get("minor", 0),
        alignment_degraded=raw_summary.get("alignment_degraded", False),
    )


def _build_response(comparison: object, created: bool = False) -> ComparisonCreateResponse:
    """Build a ComparisonCreateResponse from a DocumentComparison ORM instance."""
    return ComparisonCreateResponse(
        id=comparison.id,  # type: ignore[attr-defined]
        organization_id=comparison.organization_id,  # type: ignore[attr-defined]
        document_a_version_id=comparison.document_a_version_id,  # type: ignore[attr-defined]
        document_b_version_id=comparison.document_b_version_id,  # type: ignore[attr-defined]
        status=comparison.status,  # type: ignore[attr-defined]
        summary=_build_summary(comparison),
        error_message=comparison.error_message,  # type: ignore[attr-defined]
        requested_by=comparison.requested_by,  # type: ignore[attr-defined]
        created_at=comparison.created_at,  # type: ignore[attr-defined]
        completed_at=comparison.completed_at,  # type: ignore[attr-defined]
        created=created,
    )


def _build_plain_response(comparison: object) -> ComparisonResponse:
    """Build a ComparisonResponse (no ``created`` flag)."""
    return ComparisonResponse(
        id=comparison.id,  # type: ignore[attr-defined]
        organization_id=comparison.organization_id,  # type: ignore[attr-defined]
        document_a_version_id=comparison.document_a_version_id,  # type: ignore[attr-defined]
        document_b_version_id=comparison.document_b_version_id,  # type: ignore[attr-defined]
        status=comparison.status,  # type: ignore[attr-defined]
        summary=_build_summary(comparison),
        error_message=comparison.error_message,  # type: ignore[attr-defined]
        requested_by=comparison.requested_by,  # type: ignore[attr-defined]
        created_at=comparison.created_at,  # type: ignore[attr-defined]
        completed_at=comparison.completed_at,  # type: ignore[attr-defined]
    )


async def _authorize_both_sides(
    user: object,
    comparison: object,
    db: AsyncSession,
) -> None:
    """Re-check per-version authorization for BOTH sides (plan §13).

    A document_comparisons row can outlive a later access_level change on
    either source document, so every read re-runs the per-version authorizer.
    Any failure (missing / cross-org / forbidden) raises NotFoundError —
    never a differentiated status code that could leak existence.
    """
    await AuthorizationService.authorize_document_version(
        user,  # type: ignore[arg-type]
        comparison.document_a_version_id,  # type: ignore[attr-defined]
        db,
    )
    await AuthorizationService.authorize_document_version(
        user,  # type: ignore[arg-type]
        comparison.document_b_version_id,  # type: ignore[attr-defined]
        db,
    )


async def _resolve_source_refs(
    db: AsyncSession,
    changes: list,
) -> dict[str, SourceRef]:
    """Resolve old/new chunk IDs into display provenance (plan §9.9/§13).

    Delegates to the shared ComparisonService chunk-provenance join.
    Chunks deleted since the comparison ran (FK SET NULL) simply have no
    entry — the UI degrades to the denormalized text.
    """
    chunk_ids: list[str] = []
    for c in changes:
        if c.old_chunk_id:
            chunk_ids.append(c.old_chunk_id)
        if c.new_chunk_id:
            chunk_ids.append(c.new_chunk_id)
    if not chunk_ids:
        return {}

    provenance = await ComparisonService.resolve_chunk_provenance(db, chunk_ids)
    return {
        chunk_id: SourceRef(
            document_id=p["document_id"],
            document_version_id=p["document_version_id"],
            document_name=p["document_name"],
            version_number=p["version_number"],
            page_number=p["page_number"],
            section=p["section"],
        )
        for chunk_id, p in provenance.items()
    }


def _build_change_response(
    change: object,
    refs: dict[str, SourceRef],
) -> ComparisonChangeResponse:
    return ComparisonChangeResponse(
        id=change.id,  # type: ignore[attr-defined]
        comparison_id=change.comparison_id,  # type: ignore[attr-defined]
        change_type=change.change_type,  # type: ignore[attr-defined]
        severity=change.severity,  # type: ignore[attr-defined]
        section=change.section,  # type: ignore[attr-defined]
        old_chunk_id=change.old_chunk_id,  # type: ignore[attr-defined]
        new_chunk_id=change.new_chunk_id,  # type: ignore[attr-defined]
        old_text=change.old_text,  # type: ignore[attr-defined]
        new_text=change.new_text,  # type: ignore[attr-defined]
        truncated=change.truncated,  # type: ignore[attr-defined]
        created_at=change.created_at,  # type: ignore[attr-defined]
        old_source=refs.get(change.old_chunk_id) if change.old_chunk_id else None,  # type: ignore[attr-defined]
        new_source=refs.get(change.new_chunk_id) if change.new_chunk_id else None,  # type: ignore[attr-defined]
    )


async def _get_authorized_comparison(
    comparison_id: str,
    user: object,
    db: AsyncSession,
) -> object:
    """Load the comparison (org-scoped) + re-authorize both sides — or 404."""
    repo = DocumentComparisonRepository(db)
    comparison = await repo.get_by_id_for_org(
        comparison_id, user.organization_id  # type: ignore[attr-defined]
    )
    if comparison is None:
        raise NotFoundError("Comparison not found.")
    await _authorize_both_sides(user, comparison, db)
    return comparison


# ── POST /documents/compare ────────────────────────────────────────────────────

@router.post(
    "/compare",
    response_model=ComparisonCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate a document comparison",
    description=(
        "Initiates a comparison between two document versions in the same organization. "
        "Idempotent: returns the existing comparison if one already exists for the same pair. "
        "The caller polls GET /documents/compare/{id} for completion.\n\n"
        "**Business rules:**\n"
        "- Requires the `comparison:create` permission (Admin/Editor).\n"
        "- Both versions must be READY (not still processing or failed).\n"
        "- Both versions must be accessible to the requesting user (checked "
        "for BOTH sides before anything is persisted or looked up).\n"
        "- The pair is normalized so the same two versions always resolve to the same row.\n"
        "- A COMPARISON processing job is dispatched on first creation.\n"
        "- `created=false` means an existing comparison was reused (no new job)."
    ),
)
async def initiate_comparison(
    body: CompareRequest,
    response: Response,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("comparison:create"))],
) -> ComparisonCreateResponse:
    comparison, created = await ComparisonService.get_or_create_comparison(
        user=user,  # type: ignore[arg-type]
        document_a_version_id=body.document_a_version_id,
        document_b_version_id=body.document_b_version_id,
        db=db,
    )
    # §13: 202 Accepted for a NEWLY created comparison (job enqueued);
    # 200 when an existing row is returned (nothing was recomputed).
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    logger.info(
        "POST /documents/compare: comparison=%s created=%s user=%s",
        comparison.id, created, user.id,  # type: ignore[attr-defined]
    )
    return _build_response(comparison, created=created)


# ── GET /documents/compare/{comparison_id} ─────────────────────────────────────

@router.get(
    "/compare/{comparison_id}",
    response_model=ComparisonResponse,
    summary="Get comparison status",
    description=(
        "Returns the current status and summary for a document comparison. "
        "Authorization is re-checked for BOTH source versions on every read."
    ),
)
async def get_comparison(
    comparison_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
) -> ComparisonResponse:
    comparison = await _get_authorized_comparison(comparison_id, user, db)
    return _build_plain_response(comparison)


# ── GET /documents/compare/{comparison_id}/changes ─────────────────────────────

@router.get(
    "/compare/{comparison_id}/changes",
    response_model=ComparisonChangesResponse,
    summary="List comparison changes",
    description=(
        "Returns the list of detected changes for a comparison. "
        "Can be filtered by severity (MAJOR, MODERATE, MINOR) or section name. "
        "Returned unpaginated by design — a single comparison is bounded by "
        "section count (plan §13 deliberate simplification)."
    ),
)
async def list_comparison_changes(
    comparison_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: MAJOR, MODERATE, or MINOR.",
        pattern="^(MAJOR|MODERATE|MINOR)$",
    ),
    section: Optional[str] = Query(
        None,
        description="Filter by section name or label.",
    ),
) -> ComparisonChangesResponse:
    comparison = await _get_authorized_comparison(comparison_id, user, db)

    repo = DocumentComparisonRepository(db)
    changes = await repo.list_changes(
        comparison_id, severity=severity, section=section
    )
    refs = await _resolve_source_refs(db, changes)

    return ComparisonChangesResponse(
        comparison_id=comparison_id,
        total=len(changes),
        items=[_build_change_response(c, refs) for c in changes],
    )


# ── GET /documents/compare/{comparison_id}/changes/{change_id} ────────────────

@router.get(
    "/compare/{comparison_id}/changes/{change_id}",
    response_model=ComparisonChangeResponse,
    summary="Get one comparison change",
    description=(
        "Returns a single change with its resolved old/new source provenance "
        "(document, version, page, section) for the View Sources flow."
    ),
)
async def get_comparison_change(
    comparison_id: str,
    change_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
) -> ComparisonChangeResponse:
    await _get_authorized_comparison(comparison_id, user, db)

    repo = DocumentComparisonRepository(db)
    change = await repo.get_change(comparison_id, change_id)
    if change is None:
        raise NotFoundError("Comparison change not found.")
    refs = await _resolve_source_refs(db, [change])
    return _build_change_response(change, refs)


# ── GET /documents/compare/{comparison_id}/narration ──────────────────────────

@router.get(
    "/compare/{comparison_id}/narration",
    response_model=ComparisonNarrationResponse,
    summary="Get AI-generated comparison narration",
    description=(
        "Returns an LLM-generated natural-language summary of the detected changes. "
        "Only available when the comparison status is COMPLETED. "
        "Falls back to a structured plain-text summary if the LLM call fails."
    ),
)
async def get_comparison_narration(
    comparison_id: str,
    db: DbSession,
    user: Annotated[object, Depends(require_permission("document:read"))],
) -> ComparisonNarrationResponse:
    comparison = await _get_authorized_comparison(comparison_id, user, db)

    if comparison.status != "COMPLETED":  # type: ignore[attr-defined]
        from app.core.exceptions import ValidationError
        raise ValidationError(
            f"Comparison is not yet completed (status={comparison.status}). "  # type: ignore[attr-defined]
            "Poll GET /documents/compare/{id} until status is COMPLETED."
        )

    repo = DocumentComparisonRepository(db)
    changes = await repo.list_changes(comparison_id)

    from app.infrastructure.llm import get_llm_provider
    from app.rag.comparison_narration import _fallback_narration, narrate_changes

    llm_provider = get_llm_provider()
    llm_narrated = True

    if llm_provider is None:
        narration = _fallback_narration(changes)
        llm_narrated = False
    else:
        try:
            narration = await narrate_changes(changes, provider=llm_provider)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Narration LLM call failed for comparison %s — using fallback",
                comparison_id,
            )
            narration = _fallback_narration(changes)
            llm_narrated = False

    logger.info(
        "GET /documents/compare/%s/narration: changes=%d llm_narrated=%s user=%s",
        comparison_id, len(changes), llm_narrated, user.id,  # type: ignore[attr-defined]
    )

    return ComparisonNarrationResponse(
        comparison_id=comparison_id,
        narration=narration,
        llm_narrated=llm_narrated,
    )
