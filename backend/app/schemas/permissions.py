"""
Document permission schemas (Phase 16 — RESTRICTED grant management).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GrantCreateRequest(BaseModel):
    """Body of POST /documents/{id}/permissions."""

    user_id: str = Field(..., description="Grantee user UUID (same organization)")
    permission_type: Literal["read", "write", "admin"] = "read"
    expires_at: datetime | None = Field(
        None, description="Optional expiry — NULL means the grant never expires"
    )


class GrantUpdateRequest(BaseModel):
    """Body of PATCH /documents/{id}/permissions/{user_id} (optional expiry edit)."""

    permission_type: Literal["read", "write", "admin"] | None = None
    expires_at: datetime | None = None


class PermissionGrantItem(BaseModel):
    """One grant row as exposed by the list endpoint."""

    id: str
    user_id: str
    grantee_email: str | None = None
    grantee_full_name: str | None = None
    permission_type: str
    expires_at: datetime | None = None
    created_at: datetime


class PermissionListResponse(BaseModel):
    document_id: str
    items: list[PermissionGrantItem]
