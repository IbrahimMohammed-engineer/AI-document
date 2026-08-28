"""
Document, DocumentVersion, and Collection repositories.

All document data access lives here — callers (services) never write SQLAlchemy
queries directly.

Tenant isolation is structural: every public method that touches tenant-owned
data requires `organization_id` as an explicit, non-optional parameter.
The `_org_filter()` helper from TenantScopedRepository guarantees the WHERE
clause is always present.

See Backend-Architecture-Documentation.md §9 (Repository Layer).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select, update

from app.models.document import (
    Collection,
    CollectionDocument,
    Document,
    DocumentTag,
    DocumentVersion,
)
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = logging.getLogger(__name__)


# ── DocumentRepository ────────────────────────────────────────────────────────

class DocumentRepository(TenantScopedRepository[Document]):
    """Repository for the `documents` table.

    INVARIANT: every read method that touches org data passes organization_id.
    """

    model = Document

    async def create(
        self,
        *,
        organization_id: str,
        owner_id: str,
        name: str,
        document_type: str,
        department: str | None = None,
        description: str | None = None,
        access_level: str = "organization",
    ) -> Document:
        """Insert a new document row and flush (does NOT commit)."""
        doc = Document(
            organization_id=organization_id,
            owner_id=owner_id,
            name=name,
            document_type=document_type,
            department=department,
            description=description,
            access_level=access_level,
            status="active",
        )
        return await self.add(doc)

    async def get_by_id_for_org(
        self,
        id: str | UUID,
        organization_id: str | UUID,
        include_deleted: bool = False,
    ) -> Document | None:
        """Fetch a document by PK scoped to the organization.

        Returns None (never raises) if the row is in a different org or missing.
        Excludes soft-deleted rows by default — pass include_deleted=True to
        fetch deleted documents (e.g., for restore).
        """
        stmt = select(Document).where(
            Document.id == str(id),
            self._org_filter(organization_id),
        )
        if not include_deleted:
            stmt = stmt.where(Document.deleted_at.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_org(
        self,
        organization_id: str,
        *,
        document_type: str | None = None,
        status: str | None = None,
        access_level: str | None = None,
        owner_id: str | None = None,
        collection_id: str | None = None,
        department: str | None = None,
        search_name: str | None = None,
        # Keyset pagination: supply (cursor_created_at, cursor_id) from previous page
        cursor_created_at: datetime | None = None,
        cursor_id: str | None = None,
        limit: int = 20,
        sort_desc: bool = True,
    ) -> list[Document]:
        """List active (non-deleted) documents for an org with filtering and keyset pagination.

        Returns at most `limit` rows. Pagination: use the last row's (created_at, id)
        as the cursor for the next page.
        """
        stmt = (
            select(Document)
            .where(
                self._org_filter(organization_id),
                Document.deleted_at.is_(None),
            )
        )

        # Optional filters
        if document_type:
            stmt = stmt.where(Document.document_type == document_type)
        if status:
            stmt = stmt.where(Document.status == status)
        if access_level:
            stmt = stmt.where(Document.access_level == access_level)
        if owner_id:
            stmt = stmt.where(Document.owner_id == owner_id)
        if department:
            stmt = stmt.where(Document.department == department)
        if search_name:
            stmt = stmt.where(Document.name.ilike(f"%{search_name}%"))
        if collection_id:
            # Filter documents belonging to the specified collection
            stmt = stmt.where(
                Document.id.in_(
                    select(CollectionDocument.document_id).where(
                        CollectionDocument.collection_id == collection_id
                    )
                )
            )

        # Keyset pagination
        if cursor_created_at is not None and cursor_id is not None:
            if sort_desc:
                stmt = stmt.where(
                    or_(
                        Document.created_at < cursor_created_at,
                        and_(
                            Document.created_at == cursor_created_at,
                            Document.id < cursor_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        Document.created_at > cursor_created_at,
                        and_(
                            Document.created_at == cursor_created_at,
                            Document.id > cursor_id,
                        ),
                    )
                )

        # Sort
        if sort_desc:
            stmt = stmt.order_by(desc(Document.created_at), desc(Document.id))
        else:
            stmt = stmt.order_by(Document.created_at, Document.id)

        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete(self, document: Document) -> Document:
        """Set deleted_at to now (does NOT commit)."""
        document.deleted_at = datetime.now(tz=timezone.utc)
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def restore(self, document: Document) -> Document:
        """Clear deleted_at (does NOT commit)."""
        document.deleted_at = None
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def update_metadata(
        self,
        document: Document,
        *,
        name: str | None = None,
        description: str | None = None,
        document_type: str | None = None,
        department: str | None = None,
        access_level: str | None = None,
        status: str | None = None,
    ) -> Document:
        """Apply partial metadata updates to a document (does NOT commit)."""
        if name is not None:
            document.name = name
        if description is not None:
            document.description = description
        if document_type is not None:
            document.document_type = document_type
        if department is not None:
            document.department = department
        if access_level is not None:
            document.access_level = access_level
        if status is not None:
            document.status = status
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def update_current_version_id(
        self, document: Document, version_id: str | None
    ) -> Document:
        """Update the denormalized current_version_id pointer (does NOT commit)."""
        document.current_version_id = version_id
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def set_tags(
        self, document: Document, tags: list[str]
    ) -> None:
        """Replace the document's tags with the given list (does NOT commit).

        Deletes existing tags not in the new list; inserts new tags.
        """
        # Load existing tags
        result = await self._session.execute(
            select(DocumentTag).where(DocumentTag.document_id == document.id)
        )
        existing = {dt.tag: dt for dt in result.scalars().all()}
        new_set = set(tags)

        # Remove tags no longer in the new set
        for tag, dt in existing.items():
            if tag not in new_set:
                await self._session.delete(dt)

        # Add new tags
        for tag in new_set:
            if tag not in existing:
                self._session.add(DocumentTag(document_id=document.id, tag=tag))

        await self._session.flush()

    async def count_for_org(self, organization_id: str) -> int:
        """Return the total count of active documents for an org."""
        result = await self._session.execute(
            select(func.count(Document.id)).where(
                self._org_filter(organization_id),
                Document.deleted_at.is_(None),
            )
        )
        return result.scalar_one()


# ── DocumentVersionRepository ─────────────────────────────────────────────────

class DocumentVersionRepository(BaseRepository[DocumentVersion]):
    """Repository for the `document_versions` table.

    NOTE: document_versions is NOT directly tenant-scoped (it has no
    organization_id column — the tenant boundary is on the parent document).
    Always access versions through a document that has already been
    org-verified (e.g., DocumentRepository.get_by_id_for_org).
    """

    model = DocumentVersion

    async def create(
        self,
        *,
        document_id: str,
        version_number: int,
        storage_key: str,
        mime_type: str,
        file_size_bytes: int,
        created_by: str,
        checksum_sha256: str | None = None,
        version_label: str | None = None,
        effective_date: Any | None = None,
        status: str = "UPLOADED",
    ) -> DocumentVersion:
        """Insert a new version row and flush (does NOT commit)."""
        version = DocumentVersion(
            document_id=document_id,
            version_number=version_number,
            storage_key=storage_key,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            created_by=created_by,
            checksum_sha256=checksum_sha256,
            version_label=version_label,
            effective_date=effective_date,
            status=status,
        )
        return await self.add(version)

    async def get_for_document(
        self,
        document_id: str,
        version_number: int | None = None,
    ) -> DocumentVersion | None:
        """Get a specific version (or the latest if version_number is None)."""
        if version_number is not None:
            stmt = select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.version_number == version_number,
            )
        else:
            stmt = (
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(desc(DocumentVersion.version_number))
                .limit(1)
            )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_document(self, document_id: str) -> list[DocumentVersion]:
        """Return all versions for a document, oldest first."""
        result = await self._session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number)
        )
        return list(result.scalars().all())

    async def get_max_version_number(self, document_id: str) -> int:
        """Return the current highest version_number for a document (0 if none)."""
        result = await self._session.execute(
            select(func.max(DocumentVersion.version_number)).where(
                DocumentVersion.document_id == document_id
            )
        )
        return result.scalar_one() or 0

    async def update_status(
        self,
        version_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update a version's pipeline status (does NOT commit)."""
        values: dict[str, Any] = {"status": status}
        if error_message is not None:
            values["error_message"] = error_message
        await self._session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .values(**values)
        )
        await self._session.flush()

    async def set_file_size(self, version_id: str, size_bytes: int) -> None:
        """Record the authoritative file size (worker verification — Phase 4).

        Does NOT commit.
        """
        await self._session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .values(file_size_bytes=size_bytes)
        )
        await self._session.flush()

    async def set_page_count(self, version_id: str, page_count: int) -> None:
        """Backfill page_count after extraction completes (Phase 5).

        Does NOT commit.
        """
        await self._session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .values(page_count=page_count)
        )
        await self._session.flush()

    async def get_by_checksum_for_document(
        self, document_id: str, checksum_sha256: str
    ) -> DocumentVersion | None:
        """Return a version with this checksum under the given document (for duplicate detection)."""
        result = await self._session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.checksum_sha256 == checksum_sha256,
            )
        )
        return result.scalar_one_or_none()


# ── CollectionRepository ──────────────────────────────────────────────────────

class CollectionRepository(TenantScopedRepository[Collection]):
    """Repository for the `collections` and `collection_documents` tables."""

    model = Collection

    async def create(
        self,
        *,
        organization_id: str,
        name: str,
        description: str | None = None,
    ) -> Collection:
        """Insert a new collection and flush (does NOT commit)."""
        col = Collection(
            organization_id=organization_id,
            name=name,
            description=description,
        )
        return await self.add(col)

    async def list_for_org(self, organization_id: str) -> list[Collection]:
        """Return all collections for an organization, sorted by name."""
        result = await self._session.execute(
            select(Collection)
            .where(self._org_filter(organization_id))
            .order_by(Collection.name)
        )
        return list(result.scalars().all())

    async def get_by_name_for_org(
        self, organization_id: str, name: str
    ) -> Collection | None:
        """Find a collection by name within an organization."""
        result = await self._session.execute(
            select(Collection).where(
                self._org_filter(organization_id),
                Collection.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def add_document(
        self, collection_id: str, document_id: str
    ) -> CollectionDocument | None:
        """Add a document to a collection (idempotent — returns None if already present)."""
        existing = await self._session.execute(
            select(CollectionDocument).where(
                CollectionDocument.collection_id == collection_id,
                CollectionDocument.document_id == document_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None  # already a member
        member = CollectionDocument(
            collection_id=collection_id,
            document_id=document_id,
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def remove_document(
        self, collection_id: str, document_id: str
    ) -> bool:
        """Remove a document from a collection. Returns True if it was present."""
        result = await self._session.execute(
            select(CollectionDocument).where(
                CollectionDocument.collection_id == collection_id,
                CollectionDocument.document_id == document_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            return False
        await self._session.delete(member)
        await self._session.flush()
        return True
