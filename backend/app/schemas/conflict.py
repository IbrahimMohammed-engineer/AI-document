"""
Pydantic v2 schemas for the conflicts API (Phase 13).

Request/response models for:
  - GET  /conflicts                  — authorized conflict list (filterable)
  - GET  /conflicts/{id}             — detail embedding its evidence statements
  - POST /conflicts/{id}/resolve     — terminal REVIEWED/DISMISSED transition
  - GET  /conflicts/scan-status      — background-scan status snapshot

All response models use from_attributes = True (ORM mode) where applicable.
``priority`` and ``version_state`` are COMPUTED LIVE at read time (§12) —
never persisted columns.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Statement (embedded in the detail response — §4.6 Gap 3: no separate
#    /statements endpoint; the roadmap's literal endpoint list) ────────────────

class ConflictStatementItem(_OrmBase):
    id: str
    document_id: str
    document_version_id: str
    # Resolved by the service layer (not a direct ORM passthrough):
    document_name: str
    version_number: int
    chunk_id: str
    page_number: int
    section: Optional[str] = None
    statement_text: str
    # Static denormalized display value (DB §26).
    effective_date: Optional[date] = None
    # Computed LIVE via classify_version_state at every read (§12) — never stored.
    version_state: Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]


# ── Conflict summaries ────────────────────────────────────────────────────────

class ConflictSummary(_OrmBase):
    id: str
    topic: str
    severity: str
    status: str
    detection_method: str
    # Computed LIVE (§12): ACTIVE only when every statement's version is
    # CURRENT; LIKELY_RESOLVED otherwise.
    priority: Literal["ACTIVE", "LIKELY_RESOLVED"]
    statement_count: int
    detected_at: datetime


class ConflictDetailResponse(ConflictSummary):
    statements: list[ConflictStatementItem]
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[datetime] = None


class ConflictListResponse(BaseModel):
    items: list[ConflictSummary]


# ── Resolution request ────────────────────────────────────────────────────────

class ConflictResolveRequest(BaseModel):
    """Body for POST /conflicts/{id}/resolve.

    The two-value decision enum is the roadmap's own contract; the FE's
    richer menu labels (not-a-conflict / escalate / superseded) all map onto
    these two terminal states (§16 plan-authored simplification).
    """

    decision: Literal["REVIEWED", "DISMISSED"]
    note: Optional[str] = Field(
        None,
        description="Optional free-text review note persisted with the resolution.",
        max_length=2000,
    )


# ── Scan status ───────────────────────────────────────────────────────────────

class ScanStatusResponse(BaseModel):
    last_scan_status: Optional[str] = Field(
        None,
        description="PENDING / PROCESSING / RETRYING / COMPLETED / FAILED (latest CONFLICT_SCAN job).",
    )
    last_scan_completed_at: Optional[datetime] = None
    last_scan_conflicts_created: Optional[int] = None
    # Documents created after the last completed scan (or all active
    # documents when no scan has ever completed) — feeds the FE's
    # "N recently uploaded documents haven't been scanned yet" partial
    # banner; computed at read time, never stored (§18).
    unscanned_document_count: int
