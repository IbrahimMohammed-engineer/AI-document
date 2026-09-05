"""
ProcessingJob repository — all data access for the `processing_jobs` table.

Tenant isolation is structural: query methods touching org-owned rows require
`organization_id` explicitly (the table carries organization_id directly, so
TenantScopedRepository applies).

Callers (services, workers) never write SQLAlchemy queries directly.

See Backend-Architecture-Documentation.md §9 (Repository Layer), §23 (Background
Processing Architecture).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from app.domain.state_machines import JobStatus
from app.models.document import Document, DocumentVersion
from app.models.processing_job import ProcessingJob
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


class ProcessingJobRepository(TenantScopedRepository[ProcessingJob]):
    """Repository for the authoritative, durable `processing_jobs` records."""

    model = ProcessingJob

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        organization_id: str,
        document_version_id: str,
        job_type: str,
        max_attempts: int = 3,
    ) -> ProcessingJob:
        """Insert a new PENDING job and flush (does NOT commit).

        Must be called INSIDE the initiating transaction (e.g. alongside the
        upload's document/version inserts) — the Redis enqueue happens only
        after that transaction commits (Backend §50).
        """
        job = ProcessingJob(
            organization_id=organization_id,
            document_version_id=document_version_id,
            job_type=job_type,
            status=JobStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        return await self.add(job)

    async def create_for_comparison(
        self,
        *,
        organization_id: str,
        document_version_id: str,
        comparison_id: str,
        max_attempts: int = 3,
    ) -> ProcessingJob:
        """Insert a PENDING COMPARISON job row and flush (does NOT commit).

        Sets both ``document_version_id`` (anchor/A version) and
        ``comparison_id`` so the Phase 12 CHECK constraint is satisfied:
        ``(job_type = 'COMPARISON') = (comparison_id IS NOT NULL)`` (§10, Gap 3).
        """
        job = ProcessingJob(
            organization_id=organization_id,
            document_version_id=document_version_id,
            comparison_id=comparison_id,
            job_type="COMPARISON",
            status=JobStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        return await self.add(job)

    async def create_for_summary(
        self,
        *,
        organization_id: str,
        document_version_id: str,
        summary_id: str,
        max_attempts: int = 3,
    ) -> ProcessingJob:
        """Insert a PENDING SUMMARY job row and flush (does NOT commit).

        Phase 14: mirrors ``create_for_comparison`` exactly — sets both
        ``document_version_id`` (the summarized version) and ``summary_id``
        so the pairing CHECK is satisfied:
        ``(job_type = 'SUMMARY') = (summary_id IS NOT NULL)``.
        """
        job = ProcessingJob(
            organization_id=organization_id,
            document_version_id=document_version_id,
            summary_id=summary_id,
            job_type="SUMMARY",
            status=JobStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        return await self.add(job)

    async def create_for_extraction(
        self,
        *,
        organization_id: str,
        document_version_id: str,
        extraction_id: str,
        max_attempts: int = 3,
    ) -> ProcessingJob:
        """Insert a PENDING STRUCTURED_EXTRACTION job row and flush (no commit).

        Phase 14: distinct from the Phase 5 EXTRACTION ingestion stage —
        pairs ``extraction_id`` with the new job type so
        ``(job_type = 'STRUCTURED_EXTRACTION') = (extraction_id IS NOT NULL)``.
        """
        job = ProcessingJob(
            organization_id=organization_id,
            document_version_id=document_version_id,
            extraction_id=extraction_id,
            job_type="STRUCTURED_EXTRACTION",
            status=JobStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        return await self.add(job)

    async def create_for_org_scan(
        self,
        *,
        organization_id: str,
        max_attempts: int = 3,
    ) -> ProcessingJob:
        """Insert a PENDING org-wide CONFLICT_SCAN job row (flush, no commit).

        Phase 13 (§15): the scan is anchored to the ORGANIZATION, so
        ``document_version_id`` stays NULL (the
        ck_processing_jobs_version_required CHECK requires a version for
        every other job type).  ``checkpoint`` starts NULL — run_scan
        initializes it on first write.
        """
        job = ProcessingJob(
            organization_id=organization_id,
            document_version_id=None,
            job_type="CONFLICT_SCAN",
            status=JobStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
        )
        return await self.add(job)

    async def get_latest_for_org(
        self, organization_id: str, job_type: str
    ) -> Optional[ProcessingJob]:
        """Most recent job of one type for an org (scan-status reads)."""
        result = await self._session.execute(
            select(ProcessingJob)
            .where(
                self._org_filter(organization_id),
                ProcessingJob.job_type == job_type,
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def get_for_version(
        self,
        document_version_id: str,
        job_type: str | None = None,
    ) -> Optional[ProcessingJob]:
        """Return the most recent job for a version (optionally of one type)."""
        stmt = select(ProcessingJob).where(
            ProcessingJob.document_version_id == document_version_id
        )
        if job_type is not None:
            stmt = stmt.where(ProcessingJob.job_type == job_type)
        stmt = stmt.order_by(ProcessingJob.created_at.desc()).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_version(
        self, document_version_id: str
    ) -> Optional[ProcessingJob]:
        """Return the live (PENDING/PROCESSING/RETRYING) job for a version, if any."""
        result = await self._session.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.document_version_id == document_version_id,
                ProcessingJob.status.in_([s.value for s in JobStatus.active_states()]),
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_failed_for_version(
        self, document_version_id: str
    ) -> Optional[ProcessingJob]:
        """Return the most recent FAILED job for a version (retry target)."""
        result = await self._session.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.document_version_id == document_version_id,
                ProcessingJob.status == JobStatus.FAILED.value,
            )
            .order_by(ProcessingJob.completed_at.desc().nullslast(), ProcessingJob.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_version(
        self, document_version_id: str
    ) -> list[ProcessingJob]:
        """Return all jobs for a version, oldest first (history query)."""
        result = await self._session.execute(
            select(ProcessingJob)
            .where(ProcessingJob.document_version_id == document_version_id)
            .order_by(ProcessingJob.created_at)
        )
        return list(result.scalars().all())

    async def list_active_for_org(
        self, organization_id: str, limit: int = 100
    ) -> list[tuple[ProcessingJob, DocumentVersion, Document]]:
        """Active (PENDING/PROCESSING/RETRYING) jobs for an org with version+document.

        Drives the org-wide processing indicator (GET /documents/processing).
        """
        stmt = (
            select(ProcessingJob, DocumentVersion, Document)
            .join(
                DocumentVersion,
                DocumentVersion.id == ProcessingJob.document_version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                self._org_filter(organization_id),
                ProcessingJob.status.in_([s.value for s in JobStatus.active_states()]),
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(job, version, doc) for job, version, doc in result.all()]

    async def count_active_for_org(self, organization_id: str) -> int:
        """Count active jobs for an org (header widget badge)."""
        result = await self._session.execute(
            select(func.count(ProcessingJob.id)).where(
                self._org_filter(organization_id),
                ProcessingJob.status.in_([s.value for s in JobStatus.active_states()]),
            )
        )
        return result.scalar_one()

    # ── Reconciliation sweep queries ──────────────────────────────────────────

    async def list_jobs_for_sweep(
        self,
        *,
        pending_before: datetime,
        processing_before: datetime,
        limit: int = 200,
    ) -> list[ProcessingJob]:
        """Return jobs the sweep should consider re-enqueuing.

        Two candidate sets (Backend §23):
          - PENDING/RETRYING jobs whose updated_at is older than `pending_before`
            (enqueue-after-commit was missed, or Redis was flushed)
          - PROCESSING jobs whose updated_at is older than `processing_before`
            (worker crashed mid-job; idempotent handlers make re-execution safe)

        Whether a live Redis entry still exists is decided at enqueue time via
        job-id deduplication — the sweep only nominates candidates.
        """
        result = await self._session.execute(
            select(ProcessingJob)
            .where(
                (
                    ProcessingJob.status.in_(
                        [JobStatus.PENDING.value, JobStatus.RETRYING.value]
                    )
                    & (ProcessingJob.updated_at < pending_before)
                )
                | (
                    (ProcessingJob.status == JobStatus.PROCESSING.value)
                    & (ProcessingJob.updated_at < processing_before)
                )
            )
            .order_by(ProcessingJob.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    # ── State transitions (flush only — callers own the transaction) ──────────
    #
    # Every transition publishes a relay wake event (Phase 11 — Backend §37)
    # so any API instance streaming the document's processing SSE re-reads
    # the authoritative state immediately.  Publishing is fire-and-forget:
    # a Redis outage degrades stream latency to the SSE poll interval and
    # never breaks the pipeline.

    async def mark_processing(self, job: ProcessingJob, *, now: datetime) -> ProcessingJob:
        """PENDING/RETRYING → PROCESSING; sets started_at once, bumps attempts."""
        job.status = JobStatus.PROCESSING.value
        job.attempts += 1
        if job.started_at is None:
            job.started_at = now
        job.error_message = None
        await self._session.flush()
        await self._publish_wake(job)
        return job

    async def mark_progress(
        self,
        job: ProcessingJob,
        *,
        progress: int | None = None,
        message: str | None = None,
    ) -> ProcessingJob:
        """Write a small, frequent progress update (does NOT commit)."""
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if message is not None:
            job.progress_message = message
        await self._session.flush()
        await self._publish_wake(job)
        return job

    async def mark_retrying(
        self, job: ProcessingJob, *, error_message: str, now: datetime
    ) -> ProcessingJob:
        """PROCESSING → RETRYING (visible flaky-dependency signal, Backend §47)."""
        job.status = JobStatus.RETRYING.value
        job.error_message = error_message
        job.updated_at = now
        await self._session.flush()
        await self._publish_wake(job)
        return job

    async def mark_completed(
        self, job: ProcessingJob, *, now: datetime, progress: int = 100
    ) -> ProcessingJob:
        """PROCESSING → COMPLETED (terminal success)."""
        job.status = JobStatus.COMPLETED.value
        job.completed_at = now
        job.progress = progress
        job.error_message = None
        await self._session.flush()
        await self._publish_wake(job)
        return job

    async def mark_failed(
        self, job: ProcessingJob, *, error_message: str, now: datetime
    ) -> ProcessingJob:
        """PROCESSING → FAILED (terminal — retries exhausted or deterministic)."""
        job.status = JobStatus.FAILED.value
        job.error_message = error_message
        job.completed_at = now
        await self._session.flush()
        await self._publish_wake(job)
        return job

    @staticmethod
    async def _publish_wake(job: ProcessingJob) -> None:
        """Relay wake for this job's version channel (fail-safe — Backend §37).

        Org-wide jobs (CONFLICT_SCAN) have no version channel to wake —
        the scan-status endpoint polls processing_jobs directly.
        """
        if job.document_version_id is None:
            return
        from app.infrastructure.relay import publish_job_event

        await publish_job_event(job.document_version_id)


def utc_now() -> datetime:
    """Timezone-aware UTC now (shared by workers/services for consistency)."""
    return datetime.now(tz=timezone.utc)
