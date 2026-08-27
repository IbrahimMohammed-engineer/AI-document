"""
Pydantic v2 schemas for the document management API.

Request/response models for:
  - Document upload (multipart)
  - Document list / detail / metadata update
  - Bulk actions
  - Version list
  - Download URL
  - Collections (CRUD + membership)

All response models use from_attributes = True (ORM mode).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Shared config ─────────────────────────────────────────────────────────────

class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Upload ────────────────────────────────────────────────────────────────────

class DocumentUploadResponse(_OrmBase):
    """Response from POST /documents (upload success — always 202)."""
    document_id: str
    version_id: str
    version_number: int
    status: str = "UPLOADED"
    is_duplicate_warning: bool = False
    duplicate_version_id: Optional[str] = None


# ── Tag / Version summary models ──────────────────────────────────────────────

class DocumentVersionSummary(_OrmBase):
    """Embedded version summary inside DocumentResponse."""
    id: str
    version_number: int
    version_label: Optional[str] = None
    status: str
    mime_type: str
    file_size_bytes: int
    page_count: Optional[int] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    created_at: datetime
    created_by: str
    error_message: Optional[str] = None


class DocumentVersionDetail(DocumentVersionSummary):
    """Full version detail (used in GET /documents/{id}/versions list)."""
    document_id: str
    storage_key: str
    checksum_sha256: Optional[str] = None


# ── Document response ─────────────────────────────────────────────────────────

class DocumentResponse(_OrmBase):
    """Full document metadata + current version summary."""
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    document_type: str
    department: Optional[str] = None
    status: str
    access_level: str
    owner_id: str
    current_version_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)

    # Version info — populated by the service layer (not always loaded)
    current_version: Optional[DocumentVersionSummary] = None
    version_count: int = 0


class DocumentListItem(_OrmBase):
    """Lightweight document representation for list views."""
    id: str
    name: str
    document_type: str
    department: Optional[str] = None
    status: str
    access_level: str
    owner_id: str
    current_version_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)

    # Processing status from current version (flattened for list convenience)
    processing_status: Optional[str] = None
    page_count: Optional[int] = None


class DocumentListResponse(BaseModel):
    """Paginated document list response."""
    items: list[DocumentListItem]
    total: int
    limit: int
    has_more: bool
    # Keyset cursor for the next page (None if no more pages)
    next_cursor_created_at: Optional[datetime] = None
    next_cursor_id: Optional[str] = None


# ── Metadata update ───────────────────────────────────────────────────────────

class DocumentMetadataUpdate(BaseModel):
    """PATCH /documents/{id} request body — all fields optional."""
    name: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    document_type: Optional[str] = None
    department: Optional[str] = None
    access_level: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[list[str]] = None

    @field_validator("document_type")
    @classmethod
    def validate_document_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"policy", "procedure", "sop", "contract", "technical",
                   "regulatory", "hr", "marketing", "other"}
        if v not in allowed:
            raise ValueError(f"document_type must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("access_level")
    @classmethod
    def validate_access_level(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in {"organization", "restricted", "private"}:
            raise ValueError("access_level must be: organization | restricted | private")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in {"active", "archived"}:
            raise ValueError("status must be: active | archived")
        return v


# ── Download ──────────────────────────────────────────────────────────────────

class DownloadUrlResponse(BaseModel):
    """Response from GET /documents/{id}/download."""
    url: str
    expires_at: datetime
    mime_type: str
    file_size_bytes: int
    version_number: int


# ── Bulk actions ──────────────────────────────────────────────────────────────

class BulkActionItem(BaseModel):
    """One item in a bulk action request."""
    document_id: str


class BulkActionRequest(BaseModel):
    """POST /documents/bulk request body."""
    action: str  # "delete" | "restore" | "archive" | "tag" | "access_level"
    document_ids: list[str] = Field(min_length=1, max_length=100)
    # Action-specific params
    tags: Optional[list[str]] = None
    access_level: Optional[str] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"delete", "restore", "archive", "tag", "access_level"}
        if v not in allowed:
            raise ValueError(f"action must be one of: {', '.join(sorted(allowed))}")
        return v


class BulkActionItemResult(BaseModel):
    """Per-item result in a bulk action response."""
    document_id: str
    success: bool
    error: Optional[str] = None


class BulkActionResponse(BaseModel):
    """POST /documents/bulk response."""
    total: int
    succeeded: int
    failed: int
    results: list[BulkActionItemResult]


# ── Collections ───────────────────────────────────────────────────────────────

class CollectionCreate(BaseModel):
    """POST /collections request body."""
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None


class CollectionUpdate(BaseModel):
    """PATCH /collections/{id} request body."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class CollectionResponse(_OrmBase):
    """Collection detail response."""
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    document_count: int = 0


class CollectionListResponse(BaseModel):
    """List of collections."""
    items: list[CollectionResponse]
    total: int


class CollectionDocumentRequest(BaseModel):
    """Request body for adding a document to a collection."""
    document_id: str


# ── Document status ───────────────────────────────────────────────────────────

class DocumentStatusResponse(BaseModel):
    """GET /documents/{id}/status — processing status snapshot."""
    document_id: str
    version_id: str
    version_number: int
    status: str
    progress: Optional[int] = None  # 0–100, populated by workers
    error_message: Optional[str] = None
    current_step: Optional[str] = None
