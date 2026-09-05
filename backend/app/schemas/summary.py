"""
Pydantic v2 schemas for the document summary API (Phase 14).

Request/response models for:
  - GET  /summaries/{document_id}?version=   — get-or-create-and-poll
  - POST /summaries/{document_id}/regenerate — fresh-job regeneration

All response models use from_attributes = True (ORM mode), following
schemas/comparison.py's exact convention (snake_case field names — the FE
client mirrors these; no separate casing convention is introduced).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.13, §13.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Requests ──────────────────────────────────────────────────────────────────

class SummaryRegenerateRequest(BaseModel):
    """Request body for POST /summaries/{document_id}/regenerate."""

    version: Optional[int] = Field(
        default=None,
        description="Version number to regenerate; null = the document's current version.",
    )


# ── Responses ─────────────────────────────────────────────────────────────────

class SummaryCitation(BaseModel):
    """One inline resolved citation attached to a summary item."""

    index: int
    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str
    page_number: int
    section: Optional[str] = None
    relevance: float = 0.0
    quoted_text: str = ""
    char_start: Optional[int] = None
    char_end: Optional[int] = None


class SummaryItem(BaseModel):
    """One summary list item: clean display text + its resolved citations."""

    text: str
    citations: list[SummaryCitation] = Field(default_factory=list)


class SamplingDisclosureSchema(BaseModel):
    """The persisted sampling disclosure (roadmap exit criterion 7 —
    sampling is disclosed, never silent)."""

    sampled: bool = False
    strategy: str = "full"
    included_section_ids: list[str] = Field(default_factory=list)
    excluded_section_count: int = 0


class SummaryPayload(BaseModel):
    """The structured summary content (all fields explicit — an empty
    field is an explicit empty list, never an omitted key)."""

    executive_summary: list[SummaryItem] = Field(default_factory=list)
    key_points: list[SummaryItem] = Field(default_factory=list)
    dates: list[SummaryItem] = Field(default_factory=list)
    roles: list[SummaryItem] = Field(default_factory=list)
    requirements: list[SummaryItem] = Field(default_factory=list)
    risks: list[SummaryItem] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class SummaryResponse(_OrmBase):
    """Summary status + content (poll target for GET and POST regenerate)."""

    id: str
    document_id: str
    document_version_id: str
    status: str
    summary: Optional[SummaryPayload] = None
    sampling: Optional[SamplingDisclosureSchema] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    stale: bool = False
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
