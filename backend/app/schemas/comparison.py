"""
Pydantic v2 schemas for the document comparison API (Phase 12).

Request/response models for:
  - POST  /documents/compare           — initiate or reuse a comparison
  - GET   /documents/compare/{id}      — comparison status + summary
  - GET   /documents/compare/{id}/changes — paginated list of detected changes
  - GET   /documents/compare/{id}/narration — LLM-generated narrative

All response models use from_attributes = True (ORM mode).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Request ────────────────────────────────────────────────────────────────────

class CompareRequest(BaseModel):
    """Request body for POST /documents/compare."""
    document_a_version_id: str = Field(
        description="ID of the first document version (A side). The pair will be "
                    "normalized so the same two versions always map to the same "
                    "comparison row regardless of order."
    )
    document_b_version_id: str = Field(
        description="ID of the second document version (B side)."
    )


# ── Change response ────────────────────────────────────────────────────────────

class SourceRef(BaseModel):
    """Resolved provenance for one side of a change (plan §13).

    Populated by the service layer from the chunk FK join (chunk → page →
    section → version → document) — never stored on comparison_changes.
    ``None`` when the underlying chunk was deleted (FK SET NULL) — the
    denormalized old_text/new_text snapshot still renders.
    """
    document_id: str
    document_version_id: str
    document_name: str
    version_number: int
    page_number: int
    section: Optional[str] = None


class ComparisonChangeResponse(_OrmBase):
    """One detected change row (from comparison_changes)."""
    id: str
    comparison_id: str
    change_type: str            # ADDED | REMOVED | MODIFIED
    severity: str               # MAJOR | MODERATE | MINOR
    section: Optional[str] = None
    old_chunk_id: Optional[str] = None
    new_chunk_id: Optional[str] = None
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    truncated: bool = False
    created_at: datetime
    old_source: Optional[SourceRef] = None
    new_source: Optional[SourceRef] = None


# ── Comparison summary (embedded in ComparisonResponse) ───────────────────────

class ComparisonSummary(BaseModel):
    """Counts extracted from the JSONB summary column."""
    total: int = 0
    major: int = 0
    moderate: int = 0
    minor: int = 0
    alignment_degraded: bool = False


# ── Comparison response ────────────────────────────────────────────────────────

class ComparisonResponse(_OrmBase):
    """Full comparison metadata (status + summary)."""
    id: str
    organization_id: str
    document_a_version_id: str
    document_b_version_id: str
    # Phase 15 — resolved parent document IDs (document_versions.document_id)
    # so the UI can build "View source" deep links without an extra lookup.
    document_a_id: Optional[str] = None
    document_b_id: Optional[str] = None
    status: str
    summary: Optional[ComparisonSummary] = None
    error_message: Optional[str] = None
    requested_by: str
    created_at: datetime
    completed_at: Optional[datetime] = None


class ComparisonCreateResponse(ComparisonResponse):
    """Response from POST /documents/compare — adds the `created` flag."""
    created: bool = Field(
        description="True when a new comparison was triggered; "
                    "False when an existing comparison was reused."
    )


# ── Changes list response ──────────────────────────────────────────────────────

class ComparisonChangesResponse(BaseModel):
    """Paginated list of comparison_changes rows."""
    comparison_id: str
    total: int
    items: list[ComparisonChangeResponse]


# ── Narration response ─────────────────────────────────────────────────────────

class ComparisonNarrationResponse(BaseModel):
    """LLM-generated narrative of the comparison changes."""
    comparison_id: str
    narration: str
    # True when narration was generated from the LLM; False for the plain-text fallback
    llm_narrated: bool = True
