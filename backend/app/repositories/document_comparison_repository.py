"""
DocumentComparison and ComparisonChange repository — Phase 12.

Tenant isolation: document_comparisons carries organization_id, so this
repository extends TenantScopedRepository and every query requiring
tenant-owned access accepts organization_id explicitly.

Idempotency: ``add_change`` is called per-section by the worker (§9.4-9.9)
and must be safe to re-run — callers skip already-persisted sections by
checking ``list_section_ids_done`` before processing.

See PHASE-12-IMPLEMENTATION-PLAN.md §11 Task 5.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.comparison import ComparisonChange, DocumentComparison
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class DocumentComparisonRepository(TenantScopedRepository[DocumentComparison]):
    """All data access for document_comparisons and comparison_changes."""

    model = DocumentComparison

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        organization_id: str,
        version_a_id: str,
        version_b_id: str,
        requested_by: str,
    ) -> DocumentComparison:
        """Insert a PENDING document_comparisons row and flush (no commit).

        The caller commits as part of the same transaction that creates the
        accompanying processing_jobs row — same durable-row-then-pointer
        discipline as upload (Backend §50).
        """
        comparison = DocumentComparison(
            organization_id=organization_id,
            document_a_version_id=version_a_id,
            document_b_version_id=version_b_id,
            status="PENDING",
            requested_by=requested_by,
        )
        return await self.add(comparison)

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_by_pair(
        self,
        organization_id: str,
        version_a_id: str,
        version_b_id: str,
    ) -> Optional[DocumentComparison]:
        """Look up an existing comparison by the normalized pair (§9.3 reuse check).

        Versions must be passed in the SAME order they were normalized at
        creation time (lexicographic by (document_id, version_number), handled
        by ComparisonService.get_or_create_comparison before it calls this).
        """
        result = await self._session.execute(
            select(DocumentComparison).where(
                self._org_filter(organization_id),
                DocumentComparison.document_a_version_id == version_a_id,
                DocumentComparison.document_b_version_id == version_b_id,
            )
        )
        return result.scalar_one_or_none()

    # ── Status updates ────────────────────────────────────────────────────────

    async def update_status(
        self,
        comparison: DocumentComparison,
        status: str,
        *,
        summary: dict | None = None,
        error_message: str | None = None,
        completed_at: datetime | None = None,
    ) -> DocumentComparison:
        """Update the comparison status (flush only — caller owns the transaction)."""
        comparison.status = status
        if summary is not None:
            comparison.summary = summary
        if error_message is not None:
            comparison.error_message = error_message
        if completed_at is not None:
            comparison.completed_at = completed_at
        elif status in ("COMPLETED", "FAILED"):
            comparison.completed_at = _utc_now()
        await self._session.flush()
        return comparison

    # ── Change management ──────────────────────────────────────────────────────

    async def add_change(
        self,
        comparison_id: str,
        *,
        change_type: str,
        severity: str,
        section: str | None = None,
        old_chunk_id: str | None = None,
        new_chunk_id: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        truncated: bool = False,
    ) -> ComparisonChange:
        """Persist one comparison_changes row and flush (incremental, per-section).

        Called incrementally by the worker per matched section — never in a
        single end-of-job batch — so crashes mid-run leave already-persisted
        changes intact for resumption (Backend §49).
        """
        change = ComparisonChange(
            comparison_id=comparison_id,
            change_type=change_type,
            severity=severity,
            section=section,
            old_chunk_id=old_chunk_id,
            new_chunk_id=new_chunk_id,
            old_text=old_text,
            new_text=new_text,
            truncated=truncated,
        )
        self._session.add(change)
        await self._session.flush()
        await self._session.refresh(change)
        return change

    async def list_changes(
        self,
        comparison_id: str,
        *,
        severity: str | None = None,
        section: str | None = None,
    ) -> list[ComparisonChange]:
        """Return all (or filtered) changes for a comparison, oldest first."""
        stmt = select(ComparisonChange).where(
            ComparisonChange.comparison_id == comparison_id,
        )
        if severity is not None:
            stmt = stmt.where(ComparisonChange.severity == severity)
        if section is not None:
            stmt = stmt.where(ComparisonChange.section == section)
        stmt = stmt.order_by(ComparisonChange.created_at)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_change(
        self,
        comparison_id: str,
        change_id: str,
    ) -> Optional[ComparisonChange]:
        """Fetch a single change row, scoped to the comparison."""
        result = await self._session.execute(
            select(ComparisonChange).where(
                ComparisonChange.comparison_id == comparison_id,
                ComparisonChange.id == change_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_done_sections(self, comparison_id: str) -> set[str]:
        """Return the set of section names/labels already persisted for this comparison.

        Used by the worker to skip sections already processed on a resume
        (idempotency / resumability — Backend §49).
        """
        rows = await self.list_changes(comparison_id)
        done: set[str] = set()
        for row in rows:
            if row.section:
                done.add(row.section)
        return done

    async def count_by_severity(self, comparison_id: str) -> dict[str, int]:
        """Return a {severity: count} summary dict for the comparison."""
        changes = await self.list_changes(comparison_id)
        counts: dict[str, int] = {"MAJOR": 0, "MODERATE": 0, "MINOR": 0}
        for c in changes:
            counts[c.severity] = counts.get(c.severity, 0) + 1
        return counts
