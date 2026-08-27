"""
DocumentService — core document lifecycle business logic.

Implements the full document management use cases:
  - upload_document():     multipart upload → storage first → DB rows → 202
  - get_document():        detail with current version + version count
  - list_documents():      filtered, paginated list
  - update_metadata():     partial update with selective audit events
  - soft_delete():         marks deleted_at; no synchronous purge
  - restore():             clears deleted_at within the grace window
  - download_url():        authorizes then issues signed URL (never proxies bytes)
  - get_version_list():    all versions for a document
  - bulk_action():         per-item service call; returns per-item result list
  - upload_new_version():  version_number = max+1 in the same transaction

All file bytes are stored in object storage BEFORE any DB row is created
(Backend §17.1 — the ordering rule that prevents phantom DB rows on storage outage).

Every method that changes state emits an audit event via AuditLogger.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    AppException,
    ConflictError,
    NotFoundError,
    StorageUnavailableError,
    ValidationError,
)
from app.domain.documents import (
    AccessLevel,
    FileValidationError,
    VersionStatus,
    compute_sha256,
    compute_storage_key,
    get_extension,
    validate_file_size,
    validate_file_type,
)
from app.infrastructure.storage import get_storage_provider
from app.models.document import Document, DocumentVersion
from app.models.user import User
from app.repositories.document_repository import (
    CollectionRepository,
    DocumentRepository,
    DocumentVersionRepository,
)
from app.schemas.document import (
    BulkActionItemResult,
    BulkActionResponse,
    DocumentListItem,
    DocumentListResponse,
    DocumentMetadataUpdate,
    DocumentResponse,
    DocumentStatusResponse,
    DocumentUploadResponse,
    DocumentVersionDetail,
    DocumentVersionSummary,
    DownloadUrlResponse,
)
from app.services.audit_logger import AuditAction, AuditLogger

logger = logging.getLogger(__name__)


def _doc_to_list_item(doc: Document, version: DocumentVersion | None) -> DocumentListItem:
    """Convert a Document ORM instance to the list-view schema."""
    return DocumentListItem(
        id=doc.id,
        name=doc.name,
        document_type=doc.document_type,
        department=doc.department,
        status=doc.status,
        access_level=doc.access_level,
        owner_id=doc.owner_id,
        current_version_id=doc.current_version_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        tags=[dt.tag for dt in (doc.tags or [])],
        processing_status=version.status if version else None,
        page_count=version.page_count if version else None,
    )


def _doc_to_response(
    doc: Document,
    version: DocumentVersion | None,
    version_count: int,
) -> DocumentResponse:
    """Convert a Document ORM instance to the full detail schema."""
    cv = None
    if version:
        cv = DocumentVersionSummary(
            id=version.id,
            version_number=version.version_number,
            version_label=version.version_label,
            status=version.status,
            mime_type=version.mime_type,
            file_size_bytes=version.file_size_bytes,
            page_count=version.page_count,
            effective_date=version.effective_date,
            expiration_date=version.expiration_date,
            created_at=version.created_at,
            created_by=version.created_by,
            error_message=version.error_message,
        )
    return DocumentResponse(
        id=doc.id,
        organization_id=doc.organization_id,
        name=doc.name,
        description=doc.description,
        document_type=doc.document_type,
        department=doc.department,
        status=doc.status,
        access_level=doc.access_level,
        owner_id=doc.owner_id,
        current_version_id=doc.current_version_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        deleted_at=doc.deleted_at,
        tags=[dt.tag for dt in (doc.tags or [])],
        current_version=cv,
        version_count=version_count,
    )


class DocumentService:
    """All document management business logic.

    Methods are static (no instance state) for simplicity — the session and
    user are always passed explicitly.
    """

    # ── Upload ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def upload_document(
        *,
        file: UploadFile,
        organization_id: str,
        owner_id: str,
        name: str,
        document_type: str,
        department: str | None = None,
        description: str | None = None,
        access_level: str = "organization",
        version_label: str | None = None,
        effective_date: str | None = None,
        # Supply to add as a new version of an existing document
        existing_document_id: str | None = None,
        db: AsyncSession = None,  # type: ignore[assignment]
        request: Request | None = None,
    ) -> DocumentUploadResponse:
        """Upload a new document (or a new version of an existing one).

        Transactional ordering (Backend §17.1 — bytes-first rule):
          1. Read + validate the file
          2. Write bytes to object storage  ← durable FIRST
          3. BEGIN → INSERT rows → COMMIT   ← DB rows only after bytes are safe
          4. Return 202 Accepted

        If step 2 fails → no DB rows are created (no phantom version).
        If step 3 fails → an orphaned storage object exists (logged; reconciled
          by the Phase 16/20 orphan-cleanup job).
        """
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        # ── Read file into memory (streaming with size guard) ─────────────────
        _settings = get_settings()
        max_bytes = _settings.max_upload_file_size_mb * 1024 * 1024
        file_bytes = b""
        async for chunk in file:
            file_bytes += chunk  # type: ignore[operator]
            if len(file_bytes) > max_bytes:
                from app.core.exceptions import FileTooLargeError
                raise FileTooLargeError(
                    f"File exceeds {_settings.max_upload_file_size_mb} MB limit."
                )

        if not file_bytes:
            raise ValidationError("Uploaded file is empty.")

        # ── Validate file type ────────────────────────────────────────────────
        filename = file.filename or "upload"
        content_type = file.content_type or "application/octet-stream"
        try:
            mime_type = validate_file_type(
                filename=filename,
                declared_content_type=content_type,
                first_bytes=file_bytes[:1024],
            )
            validate_file_size(len(file_bytes), max_bytes)
        except FileValidationError as exc:
            if exc.code == "FILE_TOO_LARGE":
                from app.core.exceptions import FileTooLargeError
                raise FileTooLargeError(exc.message) from exc
            from app.core.exceptions import InvalidFileTypeError
            raise InvalidFileTypeError(exc.message) from exc

        extension = get_extension(filename)
        checksum = compute_sha256(file_bytes)

        # ── Resolve or create the document row ────────────────────────────────
        is_new_version = existing_document_id is not None
        document_id_to_use: str

        if is_new_version:
            # Verify the document exists in this org
            doc = await doc_repo.get_by_id_for_org(
                existing_document_id, organization_id
            )
            if doc is None:
                raise NotFoundError("Document not found.")
            document_id_to_use = doc.id
        else:
            # Will insert a new document row inside the transaction below
            document_id_to_use = ""  # placeholder — assigned in the transaction

        # ── Duplicate detection (exact same bytes within same document) ────────
        is_duplicate_warning = False
        existing_dup_version: DocumentVersion | None = None
        if is_new_version:
            existing_dup_version = await ver_repo.get_by_checksum_for_document(
                document_id_to_use, checksum
            )
            if existing_dup_version is not None:
                # Exact duplicate within the same document history → 409
                raise ConflictError(
                    f"An identical file (SHA-256: {checksum[:12]}…) already exists "
                    f"as version {existing_dup_version.version_number} of this document. "
                    "Upload a different file or use the existing version."
                )

        # ── Compute storage key (needs both doc_id and version_id) ────────────
        # We need the IDs before writing. For a new doc, generate the doc UUID now.
        from app.models.base import generate_uuid

        if not is_new_version:
            document_id_to_use = generate_uuid()

        version_id = generate_uuid()
        storage_key = compute_storage_key(
            org_id=organization_id,
            document_id=document_id_to_use,
            version_id=version_id,
            extension=extension,
        )

        # ── Step 1: Write bytes to object storage (bytes durable FIRST) ───────
        storage = get_storage_provider()
        try:
            await storage.upload(
                key=storage_key,
                data=file_bytes,
                content_type=mime_type,
            )
        except Exception as exc:
            logger.exception(
                "Storage upload failed",
                extra={"storage_key": storage_key, "org": organization_id},
            )
            raise StorageUnavailableError(
                "Failed to write file to storage. Please try again."
            ) from exc

        # ── Step 2: DB transaction — INSERT document + version in one COMMIT ──
        try:
            if not is_new_version:
                new_doc = Document(
                    id=document_id_to_use,
                    organization_id=organization_id,
                    owner_id=owner_id,
                    name=name,
                    document_type=document_type,
                    department=department,
                    description=description,
                    access_level=access_level,
                    status="active",
                )
                db.add(new_doc)
                await db.flush()

            next_version_number = (
                await ver_repo.get_max_version_number(document_id_to_use)
            ) + 1

            new_version = DocumentVersion(
                id=version_id,
                document_id=document_id_to_use,
                version_number=next_version_number,
                storage_key=storage_key,
                mime_type=mime_type,
                file_size_bytes=len(file_bytes),
                checksum_sha256=checksum,
                created_by=owner_id,
                version_label=version_label,
                status="UPLOADED",
            )
            if effective_date:
                from datetime import date
                try:
                    new_version.effective_date = date.fromisoformat(effective_date)
                except ValueError:
                    pass  # silently ignore bad dates (validation is at API schema level)

            db.add(new_version)
            await db.flush()
            # If it's the first version of a new document, set current_version_id
            if not is_new_version:
                new_doc.current_version_id = version_id
                await db.flush()

            await db.commit()

        except Exception as exc:
            await db.rollback()
            logger.exception(
                "DB transaction failed after successful storage upload "
                "(orphaned object: %s)",
                storage_key,
            )
            raise

        # ── Audit event ───────────────────────────────────────────────────────
        action = (
            AuditAction.DOCUMENT_VERSION_UPLOADED
            if is_new_version
            else AuditAction.DOCUMENT_UPLOADED
        )
        try:
            async with db.begin():
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=owner_id,
                    action=action,
                    resource_type="document",
                    resource_id=document_id_to_use,
                    metadata={
                        "version_id": version_id,
                        "version_number": next_version_number,
                        "storage_key": storage_key,
                        "mime_type": mime_type,
                        "file_size_bytes": len(file_bytes),
                    },
                    request=request,
                )
        except Exception:
            # Audit failure must not fail the upload
            logger.exception("Audit log write failed for upload %s", version_id)

        return DocumentUploadResponse(
            document_id=document_id_to_use,
            version_id=version_id,
            version_number=next_version_number,
            status="UPLOADED",
            is_duplicate_warning=is_duplicate_warning,
        )

    # ── Read ───────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_document(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> DocumentResponse:
        """Fetch document detail + current version + version count."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        # Load tags
        from sqlalchemy import select
        from app.models.document import DocumentTag
        result = await db.execute(
            select(DocumentTag).where(DocumentTag.document_id == doc.id)
        )
        doc.tags = list(result.scalars().all())

        # Load current version
        current_ver: DocumentVersion | None = None
        if doc.current_version_id:
            current_ver = await ver_repo.get_by_id(doc.current_version_id)

        # Count versions
        versions = await ver_repo.list_for_document(doc.id)
        version_count = len(versions)

        return _doc_to_response(doc, current_ver, version_count)

    @staticmethod
    async def list_documents(
        *,
        organization_id: str,
        document_type: str | None = None,
        status: str | None = None,
        access_level: str | None = None,
        owner_id: str | None = None,
        collection_id: str | None = None,
        department: str | None = None,
        search_name: str | None = None,
        cursor_created_at: datetime | None = None,
        cursor_id: str | None = None,
        limit: int = 20,
        sort_desc: bool = True,
        db: AsyncSession = None,  # type: ignore[assignment]
    ) -> DocumentListResponse:
        """Filtered, keyset-paginated document list."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        docs = await doc_repo.list_for_org(
            organization_id,
            document_type=document_type,
            status=status,
            access_level=access_level,
            owner_id=owner_id,
            collection_id=collection_id,
            department=department,
            search_name=search_name,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            limit=limit + 1,  # fetch one extra to determine has_more
            sort_desc=sort_desc,
        )

        has_more = len(docs) > limit
        docs = docs[:limit]

        # Load tags and current version status for each doc
        from sqlalchemy import select, and_
        from app.models.document import DocumentTag
        items: list[DocumentListItem] = []
        for doc in docs:
            # Tags
            result = await db.execute(
                select(DocumentTag).where(DocumentTag.document_id == doc.id)
            )
            doc.tags = list(result.scalars().all())

            # Current version (for processing_status / page_count)
            version = None
            if doc.current_version_id:
                version = await ver_repo.get_by_id(doc.current_version_id)

            items.append(_doc_to_list_item(doc, version))

        # Total count for the org (unfiltered — for display)
        total = await doc_repo.count_for_org(organization_id)

        next_cursor_created_at = None
        next_cursor_id = None
        if has_more and docs:
            last = docs[-1]
            next_cursor_created_at = last.created_at
            next_cursor_id = last.id

        return DocumentListResponse(
            items=items,
            total=total,
            limit=limit,
            has_more=has_more,
            next_cursor_created_at=next_cursor_created_at,
            next_cursor_id=next_cursor_id,
        )

    @staticmethod
    async def get_version_list(
        *,
        document_id: str,
        organization_id: str,
        db: AsyncSession,
    ) -> list[DocumentVersionDetail]:
        """Return all versions for a document (org-verified first)."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        versions = await ver_repo.list_for_document(doc.id)
        return [
            DocumentVersionDetail(
                id=v.id,
                document_id=v.document_id,
                version_number=v.version_number,
                version_label=v.version_label,
                status=v.status,
                mime_type=v.mime_type,
                file_size_bytes=v.file_size_bytes,
                page_count=v.page_count,
                effective_date=v.effective_date,
                expiration_date=v.expiration_date,
                created_at=v.created_at,
                created_by=v.created_by,
                error_message=v.error_message,
                storage_key=v.storage_key,
                checksum_sha256=v.checksum_sha256,
            )
            for v in versions
        ]

    @staticmethod
    async def get_document_status(
        *,
        document_id: str,
        organization_id: str,
        db: AsyncSession,
    ) -> DocumentStatusResponse:
        """Return the processing status of the document's latest version."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        version = await ver_repo.get_for_document(doc.id)
        if version is None:
            raise NotFoundError("No versions found for this document.")

        return DocumentStatusResponse(
            document_id=doc.id,
            version_id=version.id,
            version_number=version.version_number,
            status=version.status,
            error_message=version.error_message,
        )

    # ── Download ───────────────────────────────────────────────────────────────

    @staticmethod
    async def get_download_url(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        version_number: int | None = None,
        db: AsyncSession,
        request: Request | None = None,
    ) -> DownloadUrlResponse:
        """Authorize access then return a short-lived signed URL.

        The backend NEVER proxies file bytes — the client fetches directly
        from object storage via the signed URL.
        """
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        version = await ver_repo.get_for_document(doc.id, version_number)
        if version is None:
            raise NotFoundError("Requested version not found.")

        storage = get_storage_provider()
        expires_in = get_settings().signed_url_expires_seconds
        signed_url = await storage.generate_signed_url(
            key=version.storage_key,
            expires_in_seconds=expires_in,
        )

        # Audit download (every download is audited — no session debouncing)
        try:
            async with db.begin():
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=user_id,
                    action=AuditAction.DOCUMENT_DOWNLOADED,
                    resource_type="document",
                    resource_id=document_id,
                    metadata={
                        "version_id": version.id,
                        "version_number": version.version_number,
                    },
                    request=request,
                )
        except Exception:
            logger.exception("Audit log failed for download %s", document_id)

        from datetime import timedelta
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)
        return DownloadUrlResponse(
            url=signed_url,
            expires_at=expires_at,
            mime_type=version.mime_type,
            file_size_bytes=version.file_size_bytes,
            version_number=version.version_number,
        )

    # ── Metadata update ────────────────────────────────────────────────────────

    @staticmethod
    async def update_metadata(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        update: DocumentMetadataUpdate,
        db: AsyncSession,
        request: Request | None = None,
    ) -> DocumentResponse:
        """Partial metadata update. Emits ACCESS_LEVEL_CHANGED audit when applicable."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        previous_access_level = doc.access_level
        access_level_changed = (
            update.access_level is not None
            and update.access_level != previous_access_level
        )

        async with db.begin():
            await doc_repo.update_metadata(
                doc,
                name=update.name,
                description=update.description,
                document_type=update.document_type,
                department=update.department,
                access_level=update.access_level,
                status=update.status,
            )
            if update.tags is not None:
                await doc_repo.set_tags(doc, update.tags)

            # Audit access-level change separately (it's a security event)
            if access_level_changed:
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=user_id,
                    action=AuditAction.ACCESS_LEVEL_CHANGED,
                    resource_type="document",
                    resource_id=document_id,
                    metadata={
                        "previous_access_level": previous_access_level,
                        "new_access_level": update.access_level,
                    },
                    request=request,
                )
            else:
                await AuditLogger.log(
                    db,
                    organization_id=organization_id,
                    user_id=user_id,
                    action=AuditAction.DOCUMENT_METADATA_UPDATED,
                    resource_type="document",
                    resource_id=document_id,
                    metadata=update.model_dump(exclude_none=True),
                    request=request,
                )

        # Reload tags
        from sqlalchemy import select
        from app.models.document import DocumentTag
        result = await db.execute(
            select(DocumentTag).where(DocumentTag.document_id == doc.id)
        )
        doc.tags = list(result.scalars().all())

        current_ver = None
        if doc.current_version_id:
            current_ver = await ver_repo.get_by_id(doc.current_version_id)

        versions = await ver_repo.list_for_document(doc.id)
        return _doc_to_response(doc, current_ver, len(versions))

    # ── Soft delete / restore ──────────────────────────────────────────────────

    @staticmethod
    async def soft_delete(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> None:
        """Mark document as deleted (sets deleted_at)."""
        doc_repo = DocumentRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        async with db.begin():
            await doc_repo.soft_delete(doc)
            await AuditLogger.log(
                db,
                organization_id=organization_id,
                user_id=user_id,
                action=AuditAction.DOCUMENT_DELETED,
                resource_type="document",
                resource_id=document_id,
                metadata={"name": doc.name},
                request=request,
            )

    @staticmethod
    async def restore(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> DocumentResponse:
        """Restore a soft-deleted document."""
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)

        # include_deleted=True so we can find it even though it's soft-deleted
        doc = await doc_repo.get_by_id_for_org(
            document_id, organization_id, include_deleted=True
        )
        if doc is None:
            raise NotFoundError("Document not found.")
        if doc.deleted_at is None:
            raise ConflictError("Document is not deleted.")

        async with db.begin():
            await doc_repo.restore(doc)
            await AuditLogger.log(
                db,
                organization_id=organization_id,
                user_id=user_id,
                action=AuditAction.DOCUMENT_RESTORED,
                resource_type="document",
                resource_id=document_id,
                metadata={"name": doc.name},
                request=request,
            )

        # Reload tags
        from sqlalchemy import select
        from app.models.document import DocumentTag
        result = await db.execute(
            select(DocumentTag).where(DocumentTag.document_id == doc.id)
        )
        doc.tags = list(result.scalars().all())

        current_ver = None
        if doc.current_version_id:
            current_ver = await ver_repo.get_by_id(doc.current_version_id)

        versions = await ver_repo.list_for_document(doc.id)
        return _doc_to_response(doc, current_ver, len(versions))

    # ── Bulk actions ───────────────────────────────────────────────────────────

    @staticmethod
    async def bulk_action(
        *,
        action: str,
        document_ids: list[str],
        organization_id: str,
        user_id: str,
        tags: list[str] | None = None,
        access_level: str | None = None,
        db: AsyncSession,
        request: Request | None = None,
    ) -> BulkActionResponse:
        """Apply an action to multiple documents.

        Each document is processed individually with per-row authorization.
        Partial success is normal — errors for individual docs do NOT abort the
        whole operation (Backend §15).
        """
        results: list[BulkActionItemResult] = []

        for doc_id in document_ids:
            try:
                if action == "delete":
                    await DocumentService.soft_delete(
                        document_id=doc_id,
                        organization_id=organization_id,
                        user_id=user_id,
                        db=db,
                        request=request,
                    )
                elif action == "restore":
                    await DocumentService.restore(
                        document_id=doc_id,
                        organization_id=organization_id,
                        user_id=user_id,
                        db=db,
                        request=request,
                    )
                elif action in ("archive", "access_level", "tag"):
                    update_payload = DocumentMetadataUpdate()
                    if action == "archive":
                        update_payload.status = "archived"
                    elif action == "access_level" and access_level:
                        update_payload.access_level = access_level
                    elif action == "tag" and tags is not None:
                        update_payload.tags = tags
                    await DocumentService.update_metadata(
                        document_id=doc_id,
                        organization_id=organization_id,
                        user_id=user_id,
                        update=update_payload,
                        db=db,
                        request=request,
                    )
                results.append(BulkActionItemResult(document_id=doc_id, success=True))
            except (NotFoundError, ConflictError, ValidationError) as exc:
                results.append(
                    BulkActionItemResult(
                        document_id=doc_id,
                        success=False,
                        error=str(exc),
                    )
                )
            except Exception as exc:
                logger.exception("Bulk action failed for document %s", doc_id)
                results.append(
                    BulkActionItemResult(
                        document_id=doc_id,
                        success=False,
                        error="Internal error",
                    )
                )

        succeeded = sum(1 for r in results if r.success)
        return BulkActionResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
        )
