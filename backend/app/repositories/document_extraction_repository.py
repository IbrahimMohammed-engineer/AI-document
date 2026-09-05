"""
DocumentExtraction / DocumentExtractionItem repository — Phase 14.

Tenant isolation: document_extractions carries organization_id, so this
repository extends TenantScopedRepository and every query requiring
tenant-owned access accepts organization_id explicitly.

Extraction runs are APPEND-ONLY (plan §2.6 point 3): every run is a new row
(audit trail, never overwrite-in-place), so ``add_items`` is a plain bulk
insert — deliberately WITHOUT the ON CONFLICT clause that the chunk upsert
uses (re-runs are new runs, never overwrites).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.5.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.extraction import DocumentExtraction, DocumentExtractionItem
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class DocumentExtractionRepository(TenantScopedRepository[DocumentExtraction]):
    """Repository for extraction runs and their extracted items."""

    model = DocumentExtraction

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        organization_id: str,
        document_id: str,
        document_version_id: str,
        schema_key: str,
        requested_by: str,
    ) -> DocumentExtraction:
        """Insert a PENDING extraction-run row and flush (no commit).

        The caller commits as part of the same transaction that creates the
        accompanying processing_jobs row (Backend §50).
        """
        extraction = DocumentExtraction(
            organization_id=organization_id,
            document_id=document_id,
            document_version_id=document_version_id,
            schema_key=schema_key,
            status="PENDING",
            requested_by=requested_by,
        )
        return await self.add(extraction)

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_latest_completed_for_version(
        self, document_version_id: str
    ) -> Optional[DocumentExtraction]:
        """The most recent COMPLETED run for a version (chat-reuse path, §5.9)."""
        result = await self._session.execute(
            select(DocumentExtraction)
            .where(
                DocumentExtraction.document_version_id == document_version_id,
                DocumentExtraction.status == "COMPLETED",
            )
            .order_by(DocumentExtraction.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_version(
        self,
        document_version_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DocumentExtraction]:
        """Run history for a version, most recent first (§5.13 list endpoint)."""
        result = await self._session.execute(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_version_id == document_version_id)
            .order_by(DocumentExtraction.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_for_version(self, document_version_id: str) -> int:
        """Total number of runs for a version (pagination total)."""
        result = await self._session.execute(
            select(func.count(DocumentExtraction.id)).where(
                DocumentExtraction.document_version_id == document_version_id
            )
        )
        return int(result.scalar_one())

    # ── Status updates ────────────────────────────────────────────────────────

    async def update_status(
        self,
        extraction: DocumentExtraction,
        status: str,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
        error_message: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> DocumentExtraction:
        """Update the run status (flush only — caller owns the transaction)."""
        extraction.status = status
        if model is not None:
            extraction.model = model
        if prompt_version is not None:
            extraction.prompt_version = prompt_version
        if error_message is not None:
            extraction.error_message = error_message
        if prompt_tokens is not None:
            extraction.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            extraction.completion_tokens = completion_tokens
        if status in ("COMPLETED", "FAILED"):
            extraction.completed_at = _utc_now()
        await self._session.flush()
        return extraction

    # ── Item management ───────────────────────────────────────────────────────

    async def add_items(self, extraction_id: str, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk-insert extracted-item rows (append-only; plain INSERT).

        Mirrors DocumentChunkRepository.upsert_chunks' bulk-insert style but
        deliberately WITHOUT the ON CONFLICT clause — re-runs are new rows
        by design (plan §5.5).  Must be committed by the caller.

        Returns the number of rows inserted.
        """
        if not rows:
            return 0
        payload = [dict(extraction_id=extraction_id, **row) for row in rows]
        stmt = pg_insert(DocumentExtractionItem.__table__).values(payload)
        await self._session.execute(stmt)
        return len(payload)

    async def list_items(
        self,
        extraction_id: str,
        *,
        category: str | None = None,
    ) -> list[DocumentExtractionItem]:
        """Items for a run, ordered by category (fixed order) then item_index."""
        category_order = case(
            (DocumentExtractionItem.category == "requirement", 0),
            (DocumentExtractionItem.category == "risk", 1),
            (DocumentExtractionItem.category == "date", 2),
            else_=3,
        )
        stmt = (
            select(DocumentExtractionItem)
            .where(DocumentExtractionItem.extraction_id == extraction_id)
            .order_by(category_order, DocumentExtractionItem.item_index)
        )
        if category is not None:
            stmt = stmt.where(DocumentExtractionItem.category == category)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
