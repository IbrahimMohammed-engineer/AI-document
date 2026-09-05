"""
Pydantic v2 schemas for the structured-extraction API (Phase 14).

Request/response models for:
  - POST /extractions                          — create a new run
  - GET  /extractions/{extraction_id}          — run status + items
  - GET  /documents/{document_id}/extractions  — run history

All response models use from_attributes = True (ORM mode), following
schemas/comparison.py's exact convention.

See PHASE-14-IMPLEMENTATION-PLAN.md §5.13, §13.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Requests ──────────────────────────────────────────────────────────────────

class ExtractionCreateRequest(BaseModel):
    """Request body for POST /extractions."""

    document_id: str = Field(description="ID of the document to extract from.")
    version: Optional[int] = Field(
        default=None,
        description="Version number; null = the document's current version.",
    )
    schema_key: Literal["standard_v1"] = Field(
        default="standard_v1",
        description="Closed schema enum in V1 (bound anyway — never free text).",
    )


# ── Responses ─────────────────────────────────────────────────────────────────

class ExtractionItemResponse(BaseModel):
    """One extracted item with full citation-shaped provenance."""

    id: str
    category: str
    item_index: int
    label: str
    detail: dict[str, Any] = Field(default_factory=dict)
    chunk_id: str
    page_id: str
    page_number: int
    section: Optional[str] = None
    quoted_text: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    relevance_score: Optional[float] = None


class ExtractionItemsByCategory(BaseModel):
    """Items grouped by category (only present when status is COMPLETED)."""

    requirement: list[ExtractionItemResponse] = Field(default_factory=list)
    risk: list[ExtractionItemResponse] = Field(default_factory=list)
    date: list[ExtractionItemResponse] = Field(default_factory=list)
    party: list[ExtractionItemResponse] = Field(default_factory=list)


class ExtractionRunResponse(_OrmBase):
    """One extraction run (status + metadata; items on the detail endpoint)."""

    id: str
    document_id: str
    document_version_id: str
    schema_key: str
    status: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class ExtractionRunDetailResponse(ExtractionRunResponse):
    """Run detail — items included (grouped by category) when COMPLETED."""

    items: Optional[ExtractionItemsByCategory] = None


class ExtractionRunsResponse(BaseModel):
    """Paginated run history for a document (most recent first)."""

    document_id: str
    total: int
    items: list[ExtractionRunResponse]
