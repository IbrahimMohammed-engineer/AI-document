"""
DocumentSummary repository — all data access for document_summaries (Phase 14).

Tenant isolation: document_summaries carries organization_id, so this
repository extends TenantScopedRepository and every query requiring
tenant-owned access accepts organization_id explicitly (mirrors
DocumentComparisonRepository's shape).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.5.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.summary import DocumentSummary
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class DocumentSummaryRepository(TenantScopedRepository[DocumentSummary]):
    """Repository for the one-row-per-version document summaries."""

    model = DocumentSummary

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_by_version(
        self, document_version_id: str
    ) -> Optional[DocumentSummary]:
        """The version's summary row (regardless of status — callers poll)."""
        result = await self._session.execute(
            select(DocumentSummary).where(
                DocumentSummary.document_version_id == document_version_id
            )
        )
        return result.scalar_one_or_none()

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        organization_id: str,
        document_id: str,
        document_version_id: str,
        requested_by: str,
    ) -> DocumentSummary:
        """Insert a PENDING summary row and flush (no commit).

        The caller commits as part of the same transaction that creates the
        accompanying processing_jobs row — same durable-row-then-pointer
        discipline as comparisons (Backend §50).
        """
        summary = DocumentSummary(
            organization_id=organization_id,
            document_id=document_id,
            document_version_id=document_version_id,
            status="PENDING",
            requested_by=requested_by,
        )
        return await self.add(summary)

    # ── Status updates ────────────────────────────────────────────────────────

    async def update_status(
        self,
        summary: DocumentSummary,
        status: str,
        *,
        summary_json: dict[str, Any] | None = None,
        sampling: dict[str, Any] | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        error_message: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> DocumentSummary:
        """Update the summary status (flush only — caller owns the transaction).

        ``summary_json``/``sampling``/model/prompt_version/token counts are
        written only when provided, so a FAILED transition never overwrites
        the previously-completed payload (Backend §43 — the stale summary
        remains visible until a new one actually completes).
        """
        summary.status = status
        if summary_json is not None:
            summary.summary = summary_json
        if sampling is not None:
            summary.sampling = sampling
        if model is not None:
            summary.model = model
        if prompt_version is not None:
            summary.prompt_version = prompt_version
        if error_message is not None:
            summary.error_message = error_message
        if prompt_tokens is not None:
            summary.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            summary.completion_tokens = completion_tokens
        if status in ("COMPLETED", "FAILED"):
            summary.completed_at = _utc_now()
            if status == "COMPLETED":
                summary.stale = False  # cleared on the next successful regeneration
        await self._session.flush()
        return summary

    async def mark_stale(self, document_version_id: str) -> None:
        """Set stale=true for the version's summary — a no-op when absent (§4.6).

        The entire staleness mechanism: a single defensive UPDATE fired by
        the re-chunking/re-embedding retry path.  No polling, no scheduled
        job, no new job type.
        """
        await self._session.execute(
            update(DocumentSummary)
            .where(DocumentSummary.document_version_id == document_version_id)
            .values(stale=True)
        )
