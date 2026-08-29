"""
DocumentPage repository — all data access for the `document_pages` table
(Phase 5: extraction/OCR page store).

Pages are written incrementally by the EXTRACTION stage worker and read by
the pages API (workspace/debug tooling). document_pages is NOT tenant-scoped
(no organization_id column — the tenant boundary is the parent document);
callers must have already resolved the version through an org-verified
document.

Insert idempotency is structural: ON CONFLICT (document_version_id,
page_number) DO NOTHING — a resumed run never blind-re-INSERTs pages that a
crashed attempt already committed (Backend §49).

See Backend-Architecture-Documentation.md §9 (Repository Layer), §49.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.document import DocumentPage
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class DocumentPageRepository(BaseRepository[DocumentPage]):
    """Repository for the per-version extracted pages."""

    model = DocumentPage

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_max_page_number(self, document_version_id: str) -> int:
        """Highest persisted page number for a version (0 when none).

        The resume anchor: extraction continues from max+1.
        """
        result = await self._session.execute(
            select(func.max(DocumentPage.page_number)).where(
                DocumentPage.document_version_id == document_version_id
            )
        )
        return result.scalar_one() or 0

    async def count_for_version(self, document_version_id: str) -> int:
        """Number of pages persisted for a version."""
        result = await self._session.execute(
            select(func.count(DocumentPage.id)).where(
                DocumentPage.document_version_id == document_version_id
            )
        )
        return result.scalar_one()

    async def get_for_version(
        self,
        document_version_id: str,
        page_number: int,
    ) -> DocumentPage | None:
        """One page of a version (citation source panel — Phase 10)."""
        result = await self._session.execute(
            select(DocumentPage).where(
                DocumentPage.document_version_id == document_version_id,
                DocumentPage.page_number == page_number,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_version(
        self,
        document_version_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentPage]:
        """Pages in reading order (paginated for the API)."""
        stmt = (
            select(DocumentPage)
            .where(DocumentPage.document_version_id == document_version_id)
            .order_by(DocumentPage.page_number)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ── Writes ────────────────────────────────────────────────────────────────

    async def insert_pages(
        self, rows: Sequence[dict[str, Any]]
    ) -> int:
        """Bulk-insert page rows; conflicts are skipped (idempotent resume).

        Returns the number of rows actually inserted. Must be committed by
        the caller — the extractor commits per batch so a crash resumes from
        the last persisted page instead of restarting (Backend §18/§49).
        """
        if not rows:
            return 0
        # Bind to the Table (not the mapped class): the ORM-enabled insert
        # form resolves values() keys against class attributes, where
        # "metadata" collides with Base.metadata (MetaData) — the plain
        # Core insert maps it straight to the column.
        stmt = (
            pg_insert(DocumentPage.__table__)
            .values(list(rows))
            .on_conflict_do_nothing(
                constraint="uq_document_pages_version_page"
            )
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)
