"""
DocumentSection repository — all data access for the `document_sections`
table (Phase 6: structure detection / TOC tree).

document_sections is NOT tenant-scoped (no organization_id column — the
tenant boundary is the parent document); callers must have already resolved
the version through an org-verified document.

Re-run idempotency is structural: the CHUNKING stage deletes all sections
for the version and inserts the freshly detected tree inside the same
stage — a re-run overwrites, never duplicates (Backend §49). Chunks'
section_id FK is ON DELETE SET NULL, so clearing sections can never
cascade-delete retrieval units.

See Backend-Architecture-Documentation.md §9 (Repository Layer), §49.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import delete, func, select

from app.models.document import DocumentSection
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class DocumentSectionRepository(BaseRepository[DocumentSection]):
    """Repository for the per-version hierarchical section tree."""

    model = DocumentSection

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def count_for_version(self, document_version_id: str) -> int:
        """Number of sections detected for a version (0 = no structure)."""
        result = await self._session.execute(
            select(func.count(DocumentSection.id)).where(
                DocumentSection.document_version_id == document_version_id
            )
        )
        return result.scalar_one()

    async def list_for_version(
        self, document_version_id: str
    ) -> list[DocumentSection]:
        """All sections of a version in reading (sort) order — the TOC fetch."""
        result = await self._session.execute(
            select(DocumentSection)
            .where(DocumentSection.document_version_id == document_version_id)
            .order_by(DocumentSection.sort_order)
        )
        return list(result.scalars().all())

    # ── Writes ────────────────────────────────────────────────────────────────

    async def delete_for_version(self, document_version_id: str) -> int:
        """Remove all sections of a version (re-run reset). Does NOT commit."""
        result = await self._session.execute(
            delete(DocumentSection).where(
                DocumentSection.document_version_id == document_version_id
            )
        )
        return int(result.rowcount or 0)

    async def insert_sections(
        self, rows: Sequence[dict[str, Any]]
    ) -> list[DocumentSection]:
        """Bulk-insert section rows; returns the persisted ORM instances.

        Rows must carry the resolved `parent_section_id` values (the stage
        maps DetectedSection.parent_sort_order → persisted IDs). The caller
        commits.
        """
        if not rows:
            return []
        instances = [DocumentSection(**row) for row in rows]
        self._session.add_all(instances)
        await self._session.flush()
        return instances
