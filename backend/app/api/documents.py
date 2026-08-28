"""
Documents + Collections API router.

All endpoints here are:
  1. Authentication-gated (get_current_user is mandatory)
  2. Permission-gated via require_permission() dependency
  3. Org-scoped from the verified JWT (never from client-supplied params)

Endpoints:

  Documents:
    POST   /documents                         — upload (new doc or new version)
    GET    /documents                         — list with filters / pagination
    POST   /documents/bulk                    — bulk action (must come before /{id})
    GET    /documents/processing              — org-wide active jobs (Phase 4)
    GET    /documents/{id}                    — document detail
    PATCH  /documents/{id}                    — metadata update
    DELETE /documents/{id}                    — soft delete
    POST   /documents/{id}/restore            — restore soft-deleted doc
    GET    /documents/{id}/versions           — version history
    GET    /documents/{id}/download           — issue signed URL
    GET    /documents/{id}/status             — processing status (polling)
    GET    /documents/{id}/pages              — extracted pages text/OCR (Phase 5)
    GET    /documents/{id}/toc                — section tree for the TOC panel (Phase 6)
    GET    /documents/{id}/chunks             — chunks with provenance (debug, Phase 6)
    POST   /documents/{id}/retry              — retry failed stage (Phase 4)

  Collections:
    GET    /collections                       — list collections
    POST   /collections                       — create collection
    DELETE /collections/{id}                  — delete collection
    POST   /collections/{id}/documents        — add document to collection
    DELETE /collections/{id}/documents/{did}  — remove document from collection

See:
  Backend-Architecture-Documentation.md §17 (document flow)
  Database-Architecture-Design-Documentation.md §13–14, §19
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Query,
    Request,
    UploadFile,
    status,
)

from app.api.deps import DbSession, require_permission
from app.models.user import User
from app.schemas.document import (
    BulkActionRequest,
    BulkActionResponse,
    CollectionCreate,
    CollectionDocumentRequest,
    CollectionListResponse,
    CollectionResponse,
    DocumentChunksResponse,
    DocumentListResponse,
    DocumentMetadataUpdate,
    DocumentPagesResponse,
    DocumentResponse,
    DocumentStatusResponse,
    DocumentTocResponse,
    DocumentUploadResponse,
    DocumentVersionDetail,
    DownloadUrlResponse,
)
from app.schemas.processing import ProcessingListResponse, DocumentRetryResponse
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])
collections_router = APIRouter(prefix="/collections", tags=["collections"])


# ─── Upload ───────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document (or a new version of an existing one)",
)
async def upload_document(
    request: Request,
    db: DbSession,
    file: UploadFile,
    name: str = Form(..., description="Document display name"),
    document_type: str = Form(...),
    department: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    access_level: str = Form("organization"),
    version_label: Optional[str] = Form(None),
    effective_date: Optional[str] = Form(None, description="ISO date (YYYY-MM-DD)"),
    document_id: Optional[str] = Form(None, alias="documentId"),
    user: User = Depends(require_permission("document:create")),
) -> DocumentUploadResponse:
    """Upload a PDF or DOCX.

    - Omit `documentId` to create a new logical document.
    - Supply `documentId` to add a new version to an existing document.

    Returns `202 Accepted` immediately. Processing (extraction, OCR, chunking,
    embeddings) happens asynchronously via the job queue (Phase 4+).
    """
    return await DocumentService.upload_document(
        file=file,
        organization_id=user.organization_id,
        owner_id=user.id,
        name=name,
        document_type=document_type,
        department=department,
        description=description,
        access_level=access_level,
        version_label=version_label,
        effective_date=effective_date,
        existing_document_id=document_id,
        db=db,
        request=request,
    )


# ─── List ─────────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List documents with optional filters and pagination",
)
async def list_documents(
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
    document_type: Optional[str] = Query(None, alias="type"),
    status: Optional[str] = Query(None),
    access_level: Optional[str] = Query(None, alias="accessLevel"),
    owner_id: Optional[str] = Query(None, alias="ownerId"),
    collection_id: Optional[str] = Query(None, alias="collectionId"),
    department: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Name search (case-insensitive)"),
    limit: int = Query(20, ge=1, le=100),
    cursor_created_at: Optional[datetime] = Query(None, alias="cursorCreatedAt"),
    cursor_id: Optional[str] = Query(None, alias="cursorId"),
    sort_desc: bool = Query(True, alias="sortDesc"),
) -> DocumentListResponse:
    return await DocumentService.list_documents(
        organization_id=user.organization_id,
        document_type=document_type,
        status=status,
        access_level=access_level,
        owner_id=owner_id,
        collection_id=collection_id,
        department=department,
        search_name=search,
        cursor_created_at=cursor_created_at,
        cursor_id=cursor_id,
        limit=limit,
        sort_desc=sort_desc,
        db=db,
    )


# ─── Bulk action ──────────────────────────────────────────────────────────────
# IMPORTANT: /bulk must come BEFORE /{document_id} so FastAPI does not
# try to interpret the literal string "bulk" as a UUID path parameter.

@router.post(
    "/bulk",
    response_model=BulkActionResponse,
    summary="Apply an action to multiple documents",
)
async def bulk_action(
    payload: BulkActionRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> BulkActionResponse:
    return await DocumentService.bulk_action(
        action=payload.action,
        document_ids=payload.document_ids,
        organization_id=user.organization_id,
        user_id=user.id,
        tags=payload.tags,
        access_level=payload.access_level,
        db=db,
        request=request,
    )


# ─── Org-wide active processing jobs (Phase 4) ────────────────────────────────
# IMPORTANT: /processing must come BEFORE /{document_id} so FastAPI does not
# try to interpret the literal string "processing" as a UUID path parameter.

@router.get(
    "/processing",
    response_model=ProcessingListResponse,
    summary="Org-wide active processing jobs (header indicator widget)",
)
async def list_processing_jobs(
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
    limit: int = Query(100, ge=1, le=500),
) -> ProcessingListResponse:
    """All PENDING/PROCESSING/RETRYING jobs in the organization.

    Polled by the global header processing indicator (FE §5.2); SSE arrives
    in Phase 11.
    """
    return await JobService.list_org_processing(
        organization_id=user.organization_id,
        db=db,
        limit=limit,
    )


# ─── Detail ───────────────────────────────────────────────────────────────────

@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get document detail",
)
async def get_document(
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
) -> DocumentResponse:
    return await DocumentService.get_document(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


# ─── Metadata update ──────────────────────────────────────────────────────────

@router.patch(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Update document metadata",
)
async def update_document(
    document_id: str,
    payload: DocumentMetadataUpdate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> DocumentResponse:
    return await DocumentService.update_metadata(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        update=payload,
        db=db,
        request=request,
    )


# ─── Soft delete ──────────────────────────────────────────────────────────────

@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Soft-delete a document",
)
async def delete_document(
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:delete")),
) -> None:
    await DocumentService.soft_delete(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


# ─── Restore ──────────────────────────────────────────────────────────────────

@router.post(
    "/{document_id}/restore",
    response_model=DocumentResponse,
    summary="Restore a soft-deleted document",
)
async def restore_document(
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> DocumentResponse:
    return await DocumentService.restore(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


# ─── Versions ─────────────────────────────────────────────────────────────────

@router.get(
    "/{document_id}/versions",
    response_model=list[DocumentVersionDetail],
    summary="List version history for a document",
)
async def get_versions(
    document_id: str,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
) -> list[DocumentVersionDetail]:
    return await DocumentService.get_version_list(
        document_id=document_id,
        organization_id=user.organization_id,
        db=db,
    )


# ─── Download ─────────────────────────────────────────────────────────────────

@router.get(
    "/{document_id}/download",
    response_model=DownloadUrlResponse,
    summary="Get a short-lived signed URL to download the document",
)
async def get_download_url(
    document_id: str,
    request: Request,
    db: DbSession,
    version: Optional[int] = Query(None, description="Version number (omit for latest)"),
    user: User = Depends(require_permission("document:read")),
) -> DownloadUrlResponse:
    return await DocumentService.get_download_url(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        version_number=version,
        db=db,
        request=request,
    )


# ─── Status ───────────────────────────────────────────────────────────────────

@router.get(
    "/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Get processing status for the latest version",
)
async def get_document_status(
    document_id: str,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
) -> DocumentStatusResponse:
    """Polling snapshot: `{ status, currentStep, progress, errorMessage? }` plus
    live job detail. SSE upgrade arrives in Phase 11 (FE §11)."""
    return await JobService.get_document_processing_status(
        document_id=document_id,
        organization_id=user.organization_id,
        db=db,
    )


# ─── Extracted pages (Phase 5) ────────────────────────────────────────────────

@router.get(
    "/{document_id}/pages",
    response_model=DocumentPagesResponse,
    summary="Extracted page text + OCR flags for one version",
)
async def get_document_pages(
    document_id: str,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
    version: Optional[int] = Query(None, description="Version number (omit for latest)"),
    offset: int = Query(0, ge=0, alias="offset"),
    limit: int = Query(100, ge=1, le=500),
) -> DocumentPagesResponse:
    """Page-level extraction results (workspace/debug tooling).

    Returns pages persisted so far — safe to read mid-extraction (shows
    partial progress). `ocrFailed` per page drives the partial-processing
    warning in the workspace.
    """
    return await DocumentService.get_document_pages(
        document_id=document_id,
        organization_id=user.organization_id,
        version_number=version,
        offset=offset,
        limit=limit,
        db=db,
    )


# ─── Table of contents (Phase 6) ──────────────────────────────────────────────

@router.get(
    "/{document_id}/toc",
    response_model=DocumentTocResponse,
    summary="Section tree for one version (workspace TOC panel)",
)
async def get_document_toc(
    document_id: str,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
    version: Optional[int] = Query(None, description="Version number (omit for latest)"),
) -> DocumentTocResponse:
    """Hierarchical table of contents detected during chunking (Phase 6).

    An empty `items` list is the explicit "No structure detected" state
    (FE §6.5) — the workspace falls back to page-based navigation.
    """
    return await DocumentService.get_document_toc(
        document_id=document_id,
        organization_id=user.organization_id,
        version_number=version,
        db=db,
    )


# ─── Chunks (Phase 6 — internal chunk-debug tooling) ──────────────────────────

@router.get(
    "/{document_id}/chunks",
    response_model=DocumentChunksResponse,
    summary="Chunks of one version with provenance (debug tooling)",
)
async def get_document_chunks(
    document_id: UUID,
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
    version: Optional[int] = Query(None, description="Version number (omit for latest)"),
    offset: int = Query(0, ge=0, alias="offset"),
    limit: int = Query(50, ge=1, le=500),
) -> DocumentChunksResponse:
    """The retrieval units produced by the CHUNKING stage, with full
    provenance (section, heading path, pages, flags) — the chunk-quality
    debugging view that later phases (embeddings/search/citations) build on.
    """
    return await DocumentService.get_document_chunks(
        document_id=str(document_id),
        organization_id=user.organization_id,
        version_number=version,
        offset=offset,
        limit=limit,
        db=db,
    )


# ─── Retry (Phase 4 — explicit FAILED → PROCESSING action) ────────────────────

@router.post(
    "/{document_id}/retry",
    response_model=DocumentRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed processing job (explicit action)",
)
async def retry_processing(
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> DocumentRetryResponse:
    """Re-create the failed stage's job and transition FAILED → PROCESSING.

    Retry is NEVER automatic (Backend §47) — this is the deliberate user
    action. Raises 409 when there is no failed job or processing is already
    in flight.
    """
    return await JobService.retry_failed_stage(
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


# ─── Collections router ───────────────────────────────────────────────────────

@collections_router.get(
    "",
    response_model=CollectionListResponse,
    summary="List all collections in the organization",
)
async def list_collections(
    db: DbSession,
    user: User = Depends(require_permission("document:read")),
) -> CollectionListResponse:
    return await CollectionService.list_collections(
        organization_id=user.organization_id,
        db=db,
    )


@collections_router.post(
    "",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new collection",
)
async def create_collection(
    payload: CollectionCreate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:create")),
) -> CollectionResponse:
    return await CollectionService.create_collection(
        organization_id=user.organization_id,
        user_id=user.id,
        payload=payload,
        db=db,
        request=request,
    )


@collections_router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a collection (does not delete the documents)",
)
async def delete_collection(
    collection_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:delete")),
) -> None:
    await CollectionService.delete_collection(
        collection_id=collection_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


@collections_router.post(
    "/{collection_id}/documents",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Add a document to a collection",
)
async def add_document_to_collection(
    collection_id: str,
    payload: CollectionDocumentRequest,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> None:
    await CollectionService.add_document(
        collection_id=collection_id,
        document_id=payload.document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )


@collections_router.delete(
    "/{collection_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Remove a document from a collection",
)
async def remove_document_from_collection(
    collection_id: str,
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission("document:update")),
) -> None:
    await CollectionService.remove_document(
        collection_id=collection_id,
        document_id=document_id,
        organization_id=user.organization_id,
        user_id=user.id,
        db=db,
        request=request,
    )
