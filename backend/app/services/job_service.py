"""
JobService — background-job use cases (Phase 4).

Responsibilities:
  - create_for_version():   INSERT a PENDING processing_jobs row INSIDE the
                            caller's transaction (Backend §50 — the row is
                            durable in the same commit as the entity it
                            processes; never a version row without its job)
  - enqueue_after_commit(): Redis pointer AFTER the creating transaction
                            commits; failures are logged, never raised — the
                            reconciliation sweep closes the gap
  - retry_failed_stage():   the explicit, deliberate FAILED → PROCESSING
                            action (never automatic — Backend §47)
  - get_document_processing_status(): status snapshot for polling
  - list_org_processing():  org-wide active jobs (header widget)

Workers (app/workers/jobs.py) execute the handlers; this service never runs
pipeline work itself.
"""
from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.domain.state_machines import (
    JobStatus,
    JobType,
    assert_version_transition,
    get_max_attempts,
)
from app.infrastructure.queue import QUEUE_DEFAULT, enqueue_processing_job
from app.models.processing_job import ProcessingJob
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.schemas.document import DocumentStatusResponse
from app.schemas.processing import (
    DocumentRetryResponse,
    ProcessingJobItem,
    ProcessingListResponse,
)
from app.services.audit_logger import AuditAction, AuditLogger

logger = logging.getLogger(__name__)


class JobService:
    """All background-job business logic (static methods, explicit session)."""

    # ── Creation + enqueue ────────────────────────────────────────────────────

    @staticmethod
    async def create_for_version(
        db: AsyncSession,
        *,
        organization_id: str,
        document_version_id: str,
        job_type: JobType,
    ) -> ProcessingJob:
        """Insert a PENDING job row inside the CALLER's transaction (no commit).

        The initiating service (e.g. DocumentService.upload_document) calls
        this within its BEGIN…COMMIT so the job row is atomic with the entity
        rows it processes — there is never a committed version without its
        first job (Backend §50).
        """
        repo = ProcessingJobRepository(db)
        return await repo.create(
            organization_id=organization_id,
            document_version_id=document_version_id,
            job_type=job_type.value,
            max_attempts=get_max_attempts(job_type),
        )

    @staticmethod
    async def create_for_comparison(
        db: AsyncSession,
        *,
        organization_id: str,
        comparison_id: str,
        anchor_version_id: str,
    ) -> ProcessingJob:
        """Insert a PENDING COMPARISON job row inside the CALLER's transaction.

        Mirrors ``create_for_version`` but populates ``comparison_id`` for the
        Phase 12 COMPARISON job type (§10 Task 7, Gap 3).  ``anchor_version_id``
        satisfies the NOT NULL ``document_version_id`` column while carrying
        the comparison FK in ``comparison_id``.

        Must be called INSIDE the transaction that creates the
        ``document_comparisons`` row so both are committed atomically (Backend §50).
        """
        repo = ProcessingJobRepository(db)
        return await repo.create_for_comparison(
            organization_id=organization_id,
            document_version_id=anchor_version_id,
            comparison_id=comparison_id,
            max_attempts=get_max_attempts(JobType.COMPARISON),
        )

    @staticmethod
    async def create_for_summary(
        db: AsyncSession,
        *,
        organization_id: str,
        document_version_id: str,
        summary_id: str,
    ) -> ProcessingJob:
        """Insert a PENDING SUMMARY job row inside the CALLER's transaction.

        Phase 14: mirrors ``create_for_comparison`` — the job runs against an
        already-READY version (document_version_id carries the summarized
        version directly; summary_id pairs the domain row, §5.4).
        """
        repo = ProcessingJobRepository(db)
        return await repo.create_for_summary(
            organization_id=organization_id,
            document_version_id=document_version_id,
            summary_id=summary_id,
            max_attempts=get_max_attempts(JobType.SUMMARY),
        )

    @staticmethod
    async def create_for_extraction(
        db: AsyncSession,
        *,
        organization_id: str,
        document_version_id: str,
        extraction_id: str,
    ) -> ProcessingJob:
        """Insert a PENDING STRUCTURED_EXTRACTION job row inside the CALLER's tx.

        Phase 14: the new job type is distinct from the Phase 5 EXTRACTION
        ingestion stage (§2.4's name-collision correction).
        """
        repo = ProcessingJobRepository(db)
        return await repo.create_for_extraction(
            organization_id=organization_id,
            document_version_id=document_version_id,
            extraction_id=extraction_id,
            max_attempts=get_max_attempts(JobType.STRUCTURED_EXTRACTION),
        )

    @staticmethod
    async def create_for_org_scan(
        db: AsyncSession,
        *,
        organization_id: str,
        job_type: JobType = JobType.CONFLICT_SCAN,
    ) -> ProcessingJob:
        """Insert a PENDING org-wide scan job row inside the CALLER's tx (§15).

        Mirrors ``create_for_version``'s shape, adapted for the now-nullable
        ``document_version_id``: an org-wide CONFLICT_SCAN job has no single
        version anchor, so the column stays NULL (allowed only for this job
        type by ck_processing_jobs_version_required).  Queue: QUEUE_LOW —
        maintenance/housekeeping never starves ingestion bursts.
        """
        repo = ProcessingJobRepository(db)
        return await repo.create_for_org_scan(
            organization_id=organization_id,
            max_attempts=get_max_attempts(job_type),
        )

    @staticmethod
    async def enqueue_after_commit(
        job: ProcessingJob,
        *,
        queue_name: str = QUEUE_DEFAULT,
        delay_seconds: int | None = None,
    ) -> None:
        """Enqueue the Redis pointer — call ONLY after the creating commit.

        Redis being down must not fail the (already-durable) business
        operation: the failure is logged at error level and the periodic
        reconciliation sweep re-enqueues the PENDING row once Redis returns
        (Backend §51).
        """
        try:
            enqueued = await enqueue_processing_job(
                job.id, queue_name=queue_name, delay_seconds=delay_seconds
            )
            if not enqueued:
                logger.info(
                    "Job %s pointer already live (deduplicated)", job.id
                )
        except Exception:
            logger.exception(
                "REDIS ENQUEUE FAILED for job %s (%s) — row is PENDING in "
                "PostgreSQL; the reconciliation sweep will recover it",
                job.id,
                job.job_type,
            )

    # ── Explicit retry (FAILED → PROCESSING) ─────────────────────────────────

    @staticmethod
    async def retry_failed_stage(
        *,
        document_id: str,
        organization_id: str,
        user_id: str,
        db: AsyncSession,
        request: Request | None = None,
    ) -> DocumentRetryResponse:
        """Re-create the failed stage's job and transition FAILED → PROCESSING.

        Retry is an explicit user/operator action, never automatic (Backend
        §47): this creates a fresh PENDING job of the same type and flips the
        version back to PROCESSING deliberately.
        """
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)
        job_repo = ProcessingJobRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        version = await ver_repo.get_for_document(doc.id)
        if version is None:
            raise NotFoundError("No versions found for this document.")

        active = await job_repo.get_active_for_version(version.id)
        if active is not None:
            raise ConflictError(
                "Processing is already in progress for this document — "
                "no retry needed."
            )

        failed = await job_repo.get_latest_failed_for_version(version.id)
        if failed is None:
            raise ConflictError("No failed processing job to retry.")

        if version.status != "FAILED":
            raise ConflictError(
                f"Document version is {version.status} — retry applies only "
                "to failed processing."
            )

        # Explicit, validated state-machine transition FAILED → PROCESSING
        from app.domain.documents import VersionStatus

        assert_version_transition(VersionStatus.FAILED, VersionStatus.PROCESSING)

        # NOTE: the SELECTs above implicitly began a transaction — the writes
        # below join it, so commit/rollback explicitly (never db.begin()).
        try:
            new_job = await job_repo.create(
                organization_id=organization_id,
                document_version_id=version.id,
                job_type=failed.job_type,
                max_attempts=get_max_attempts(JobType(failed.job_type)),
            )
            await ver_repo.update_status(
                version.id,
                VersionStatus.PROCESSING.value,
            )
            await AuditLogger.log(
                db,
                organization_id=organization_id,
                user_id=user_id,
                action=AuditAction.PROCESSING_RETRIED,
                resource_type="document",
                resource_id=document_id,
                metadata={
                    "version_id": version.id,
                    "retried_job_id": failed.id,
                    "new_job_id": new_job.id,
                    "job_type": failed.job_type,
                },
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        # Pointer AFTER commit; sweep recovers if Redis is down
        await JobService.enqueue_after_commit(new_job)

        logger.info(
            "Retry enqueued: job %s (%s) for version %s by user %s",
            new_job.id,
            new_job.job_type,
            version.id,
            user_id,
        )
        return DocumentRetryResponse(
            job_id=new_job.id,
            job_type=new_job.job_type,
            status=new_job.status,
            version_status=VersionStatus.PROCESSING.value,
            message="Retry queued — the failed stage will re-run.",
        )

    # ── Status reads ──────────────────────────────────────────────────────────

    @staticmethod
    async def get_document_processing_status(
        *,
        document_id: str,
        organization_id: str,
        db: AsyncSession,
    ) -> DocumentStatusResponse:
        """Status snapshot for GET /documents/{id}/status (polling).

        Includes live job detail (current step / progress) from the latest
        processing_jobs row for the document's latest version.
        """
        doc_repo = DocumentRepository(db)
        ver_repo = DocumentVersionRepository(db)
        job_repo = ProcessingJobRepository(db)

        doc = await doc_repo.get_by_id_for_org(document_id, organization_id)
        if doc is None:
            raise NotFoundError("Document not found.")

        version = await ver_repo.get_for_document(doc.id)
        if version is None:
            raise NotFoundError("No versions found for this document.")

        # Prefer the live job; fall back to the most recent job of any status
        job = await job_repo.get_active_for_version(version.id)
        if job is None:
            job = await job_repo.get_for_version(version.id)

        current_step: str | None = None
        progress: int | None = None
        progress_message: str | None = None
        job_id: str | None = None
        job_status: str | None = None

        if job is not None:
            job_id = job.id
            job_status = job.status
            if job.status in (JobStatus.PENDING.value, JobStatus.PROCESSING.value,
                              JobStatus.RETRYING.value):
                current_step = job.job_type
                progress = job.progress
                progress_message = job.progress_message
            elif job.status == JobStatus.COMPLETED.value:
                progress = 100

        # A READY version is fully processed regardless of job history
        if version.status == "READY":
            progress = 100
            current_step = None

        return DocumentStatusResponse(
            document_id=doc.id,
            version_id=version.id,
            version_number=version.version_number,
            status=version.status,
            progress=progress,
            progress_message=progress_message,
            error_message=version.error_message,
            current_step=current_step,
            job_id=job_id,
            job_status=job_status,
        )

    @staticmethod
    async def list_org_processing(
        *,
        organization_id: str,
        db: AsyncSession,
        limit: int = 100,
    ) -> ProcessingListResponse:
        """Org-wide active jobs (GET /documents/processing — header widget)."""
        job_repo = ProcessingJobRepository(db)
        rows = await job_repo.list_active_for_org(organization_id, limit=limit)

        items = [
            ProcessingJobItem(
                job_id=job.id,
                document_id=doc.id,
                document_name=doc.name,
                version_id=version.id,
                version_number=version.version_number,
                job_type=job.job_type,
                status=job.status,
                progress=job.progress,
                progress_message=job.progress_message,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                started_at=job.started_at,
                created_at=job.created_at,
            )
            for job, version, doc in rows
        ]
        return ProcessingListResponse(items=items, total=len(items))
