"""
Pydantic v2 schemas for the background processing API (Phase 4).

Request/response models for:
  - GET  /documents/processing       → ProcessingListResponse (org-wide active jobs)
  - POST /documents/{id}/retry       → DocumentRetryResponse (202)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DocumentRetryResponse(BaseModel):
    """Response from POST /documents/{id}/retry (202 Accepted)."""
    job_id: str
    job_type: str
    status: str = "PENDING"
    version_status: str
    message: str


class ProcessingJobItem(BaseModel):
    """One active job in the org-wide processing list (header widget)."""
    job_id: str
    document_id: str
    document_name: str
    version_id: str
    version_number: int
    job_type: str
    status: str
    progress: Optional[int] = None
    progress_message: Optional[str] = None
    attempts: int
    max_attempts: int
    started_at: Optional[datetime] = None
    created_at: datetime


class ProcessingListResponse(BaseModel):
    """GET /documents/processing — all active jobs in the organization."""
    items: list[ProcessingJobItem]
    total: int
