"""
Worker job handlers — the async execution half of the job system.

`run_processing_job` is the single Arq job function: the Redis pointer carries
ONLY the processing_jobs UUID; everything else is loaded FRESH from PostgreSQL
on every execution (never trusted from a payload snapshot — Backend §23).

Execution pattern per roadmap Phase 4 step 4:
    claim → PROCESSING + started_at + attempts++
    → validate payload org against the referenced entity's actual org
      (tamper defense — Backend §14)
    → load the referenced version/document fresh
    → invoke the registered handler (idempotent — safe to re-run)
    → COMPLETED  |  RETRYING (backoff)  |  FAILED (exhausted → dead-letter)

Phase 5 replaced the trivial EXTRACTION handler with real text extraction +
OCR (ingestion/extractor.py); Phase 6 adds the CHUNKING handler (structure
detection + chunking) and the EXTRACTION → CHUNKING chain. The claim/
validate/retry/sweep machinery is unchanged.

Transaction discipline: handlers own their writes and may commit incrementally
(extraction commits page batches as resume anchors), so the completion /
failure transitions use `_atomic()` — which participates in the session's
active transaction when one exists and opens one otherwise — rather than
session.begin(), which raises on an already-begun transaction.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.documents import VersionStatus
from app.domain.state_machines import (
    DeterministicJobError,
    InvalidJobTransitionError,
    InvalidVersionTransitionError,
    JobStatus,
    JobType,
    assert_job_transition,
    assert_version_transition,
    get_backoff_seconds,
)
from app.infrastructure.embeddings import (
    EmbeddingDimensionError,
    EmbeddingProviderError,
    get_embedding_provider,
)
from app.infrastructure.ocr import OCRProviderUnavailable, get_ocr_provider
from app.infrastructure.queue import (
    QUEUE_DEFAULT,
    QUEUE_LOW,
    enqueue_processing_job,
    push_dead_letter,
)
from app.infrastructure.storage import (
    StorageObjectMissingError,
    get_storage_provider,
)
from app.ingestion.chunking_stage import ChunkingError, run_chunking
from app.ingestion.embedding_stage import EmbeddingError, run_embedding
from app.ingestion.extractor import run_extraction
from app.ingestion.parser import ExtractionError
from app.models.document import Document, DocumentVersion
from app.models.processing_job import ProcessingJob
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.processing_job_repository import (
    ProcessingJobRepository,
    utc_now,
)

logger = logging.getLogger(__name__)

# Handler signature: async (ctx, job, version, document, session) -> None
JobHandler = Callable[..., Awaitable[None]]


# ── Transaction scoping helper ────────────────────────────────────────────────

@asynccontextmanager
async def _atomic(session: AsyncSession) -> AsyncIterator[None]:
    """Run a block and commit, whether or not a transaction is already active.

    Handlers flush (autobegin) and may also commit internally (extraction's
    page-batch resume anchors), so the outer transitions cannot assume the
    session is transaction-less. This joins the active transaction when one
    exists (committing on success, rolling back on error) and otherwise opens
    a plain transaction block.
    """
    if session.in_transaction():
        try:
            yield
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    else:
        async with session.begin():
            yield


# ── EXTRACTION stage handler (Phase 5 — real extraction + OCR) ────────────────

async def handle_extraction(
    ctx: dict[str, Any],
    job: ProcessingJob,
    version: DocumentVersion,
    document: Document,
    session: AsyncSession,
) -> None:
    """EXTRACTION stage — real text extraction with per-page OCR routing.

    Flow (roadmap Phase 5):
      1. Verify the stored object exists + record the authoritative size.
      2. run_extraction(): magic-byte re-verification → parser selection →
         streamed per-page extraction → per-page OCR routing/tolerance →
         batched idempotent persistence → page_count backfill.
      3. Typed ExtractionErrors are DETERMINISTIC → terminal FAILED (the
         file, not the infrastructure, is the problem). A broken OCR
         configuration (e.g. Tesseract missing) is likewise deterministic.
         Transient storage faults raise generically → Phase 4 retry policy.

    Idempotency: re-runs resume from the last persisted page (ON CONFLICT DO
    NOTHING inserts) — never blind re-INSERT (Backend §49).
    """
    job_repo = ProcessingJobRepository(session)
    ver_repo = DocumentVersionRepository(session)

    await job_repo.mark_progress(job, progress=1, message="Preparing extraction…")

    # Storage durability check (Phase 4 behavior retained): fail fast with a
    # clear deterministic error when the bytes never made it to storage.
    storage = get_storage_provider()
    try:
        meta = await storage.stat(version.storage_key)
    except StorageObjectMissingError as exc:
        raise DeterministicJobError(
            f"Uploaded file object is missing from storage "
            f"(version {version.id}). Re-upload the document.",
            code="STORAGE_OBJECT_MISSING",
        ) from exc

    if meta["size"] != version.file_size_bytes:
        logger.warning(
            "Storage size mismatch for version %s (recorded=%s, actual=%s) — "
            "recording authoritative size",
            version.id,
            version.file_size_bytes,
            meta["size"],
        )
        await ver_repo.set_file_size(version.id, meta["size"])

    # Resolve the configured OCR provider up front so a broken configuration
    # (e.g. OCR_PROVIDER=tesseract without the binary) fails fast and loudly
    # instead of silently marking every scanned page OCR-failed. OCR_PROVIDER
    # =none resolves to NullOCRProvider — pages needing OCR then get the
    # explicit "OCR failed" marker (graceful degradation, Backend §19).
    try:
        ocr_provider = get_ocr_provider()
    except OCRProviderUnavailable as exc:
        raise DeterministicJobError(
            f"OCR provider misconfigured: {exc}",
            code="OCR_PROVIDER_UNAVAILABLE",
        ) from exc

    try:
        await run_extraction(
            session,
            version=version,
            document=document,
            job=job,
            job_repo=job_repo,
            ver_repo=ver_repo,
            storage=storage,
            ocr_provider=ocr_provider,
        )
    except ExtractionError as exc:
        raise DeterministicJobError(exc.message, code=exc.code) from exc

    # ── Chain the next stage (Phase 6): a CHUNKING job is created inside a
    # committed transaction, then its Redis pointer is enqueued — the same
    # durable-row-then-pointer discipline as upload (Backend §50). Until
    # Phase 7 lands, the chain ends after CHUNKING completes.
    from app.services.job_service import JobService

    async with _atomic(session):
        chunking_job = await JobService.create_for_version(
            session,
            organization_id=job.organization_id,
            document_version_id=version.id,
            job_type=JobType.CHUNKING,
        )
    await JobService.enqueue_after_commit(chunking_job)


# ── CHUNKING stage handler (Phase 6 — structure detection + chunking) ─────────

async def handle_chunking(
    ctx: dict[str, Any],
    job: ProcessingJob,
    version: DocumentVersion,
    document: Document,
    session: AsyncSession,
) -> None:
    """CHUNKING stage — heuristic structure detection + structure-aware chunking.

    Flow (roadmap Phase 6; Backend §20/§21):
      1. run_chunking(): load stored pages → font/table signals from the
         original bytes (PDF, best-effort) → section-tree detection →
         idempotent section/chunk persistence (UPSERT + batch checkpoints).
      2. No-structure documents are an explicitly supported, non-error
         state: zero sections, page/paragraph-aware chunking (Backend §20).
      3. Typed ChunkingErrors are DETERMINISTIC → terminal FAILED.
         Infrastructure faults raise generically → Phase 4 retry policy.

    Idempotency: re-runs delete + re-insert sections and UPSERT chunks on
    (document_version_id, chunk_index) — overwrite, never duplicate
    (Backend §49).

    Phase 7: chains an EMBEDDING job after successful chunking.
    """
    job_repo = ProcessingJobRepository(session)
    ver_repo = DocumentVersionRepository(session)

    await job_repo.mark_progress(job, progress=1, message="Preparing chunking…")

    try:
        await run_chunking(
            session,
            version=version,
            document=document,
            job=job,
            job_repo=job_repo,
            ver_repo=ver_repo,
        )
    except ChunkingError as exc:
        raise DeterministicJobError(exc.message, code=exc.code) from exc

    # ── Phase 14: summary staleness invalidation hook (plan §4.6) ──────────
    # The ONLY path that re-persists chunks for a version that may already
    # have a summary is a chunking retry.  Defensive, single-statement
    # UPDATE — a no-op when no summary row exists.  Never fails chunking.
    try:
        from app.repositories.document_summary_repository import (
            DocumentSummaryRepository,
        )

        await DocumentSummaryRepository(session).mark_stale(version.id)
        await session.commit()
    except Exception:  # noqa: BLE001 — staleness is best-effort bookkeeping
        logger.exception(
            "Failed to mark summaries stale for version %s — continuing", version.id
        )
        await session.rollback()

    # ── Phase 7: chain the EMBEDDING stage ────────────────────────────────
    # Create the PENDING job row first (durable), then enqueue the Redis
    # pointer — same pattern as the EXTRACTION → CHUNKING chain (Backend §50).
    from app.services.job_service import JobService

    async with _atomic(session):
        embedding_job = await JobService.create_for_version(
            session,
            organization_id=job.organization_id,
            document_version_id=version.id,
            job_type=JobType.EMBEDDING,
        )
    await JobService.enqueue_after_commit(embedding_job)


# ── EMBEDDING stage handler (Phase 7 — batched, resumable embedding) ──────────

async def handle_embedding(
    ctx: dict[str, Any],
    job: ProcessingJob,
    version: DocumentVersion,
    document: Document,
    session: AsyncSession,
) -> None:
    """EMBEDDING stage — batch-embed all chunks, write vectors incrementally.

    Flow (roadmap Phase 7; Backend §22):
      1. run_embedding(): loads unembedded chunks → calls EmbeddingProvider
         in batches → writes each batch immediately (incremental checkpoint
         so a crash never re-bills already-embedded batches — Backend §49)
         → records cost attribution.
      2. EmbeddingError (dimension mismatch, auth failure) →
         DeterministicJobError → terminal FAILED (do NOT retry — the config
         or the migration is wrong).
      3. EmbeddingProviderError (transient: network, 429, 5xx) propagates
         generically → Phase 4 retry policy.

    Idempotency: already-embedded chunks (embedding IS NOT NULL) are skipped;
    re-running embeds only the tail left by a previous crash.

    Chains the INDEXING stage on success.
    """
    job_repo = ProcessingJobRepository(session)
    ver_repo = DocumentVersionRepository(session)

    await job_repo.mark_progress(job, progress=1, message="Initialising embedding pipeline…")

    try:
        provider = get_embedding_provider()
    except RuntimeError as exc:
        raise DeterministicJobError(
            "Embedding provider not initialised — check EMBEDDING_PROVIDER config.",
            code="EMBEDDING_PROVIDER_UNAVAILABLE",
        ) from exc

    try:
        await run_embedding(
            session,
            version=version,
            document=document,
            job=job,
            job_repo=job_repo,
            ver_repo=ver_repo,
            provider=provider,
        )
    except EmbeddingError as exc:
        raise DeterministicJobError(exc.message, code=exc.code) from exc
    except EmbeddingDimensionError as exc:
        raise DeterministicJobError(str(exc), code="EMBEDDING_DIMENSION_MISMATCH") from exc
    # EmbeddingProviderError → propagates generically → retry policy

    # ── Chain the INDEXING stage ───────────────────────────────────────────
    from app.services.job_service import JobService

    async with _atomic(session):
        indexing_job = await JobService.create_for_version(
            session,
            organization_id=job.organization_id,
            document_version_id=version.id,
            job_type=JobType.INDEXING,
        )
    await JobService.enqueue_after_commit(indexing_job)


# ── INDEXING stage handler (Phase 7 — verification + READY transition) ─────────

async def handle_indexing(
    ctx: dict[str, Any],
    job: ProcessingJob,
    version: DocumentVersion,
    document: Document,
    session: AsyncSession,
) -> None:
    """INDEXING stage — verify embeddings, transition version to READY.

    Flow (roadmap Phase 7 step 10; Backend §16):
      1. Verify all chunks have a non-null embedding (guard against partial
         embedding runs that slipped through without the final check).
      2. Transition version status to READY.
      3. Update the document's ``current_version_id`` pointer if this version
         is more recent than the existing current (domain/versioning.py —
         resolve_current_version logic, applied here with the real DB rows).

    This stage is deterministic and fast (no external calls).  A failure
    here indicates a data-consistency bug and should be investigated, not
    retried blindly (retry budget = 1 per domain/state_machines.py).
    """
    job_repo = ProcessingJobRepository(session)
    ver_repo = DocumentVersionRepository(session)
    doc_repo = DocumentRepository(session)
    chunk_repo = DocumentChunkRepository(session)

    await job_repo.mark_progress(job, progress=10, message="Verifying embeddings…")

    # ── Step 1: verify all chunks are embedded ────────────────────────────
    total = await chunk_repo.count_for_version(version.id)
    embedded = await chunk_repo.count_embedded_for_version(version.id)

    if total > 0 and embedded < total:
        raise DeterministicJobError(
            f"INDEXING: only {embedded}/{total} chunks have embeddings for "
            f"version {version.id}. Re-run EMBEDDING before INDEXING.",
            code="INDEXING_INCOMPLETE_EMBEDDINGS",
        )

    logger.info(
        "INDEXING: version=%s chunks_total=%d chunks_embedded=%d — all OK",
        version.id, total, embedded,
    )

    await job_repo.mark_progress(job, progress=50, message="Setting version READY…")

    # ── Step 2: transition version to READY ───────────────────────────────
    from app.domain.documents import VersionStatus
    current_status = VersionStatus(version.status)
    assert_version_transition(current_status, VersionStatus.READY)
    await ver_repo.update_status(version.id, VersionStatus.READY.value)
    await session.commit()

    # ── Step 3: update document.current_version_id ────────────────────────
    # Load all READY versions for this document and use resolve_current_version
    # to decide if this new version should become the active pointer.
    all_versions = await ver_repo.list_for_document(version.document_id)
    ready_versions = [v for v in all_versions if v.status == VersionStatus.READY.value]

    from app.domain.versioning import resolve_current_version
    best_version = resolve_current_version(ready_versions)

    if best_version is not None:
        fresh_doc = await doc_repo.get_by_id(document.id)
        if fresh_doc is not None:
            if fresh_doc.current_version_id != best_version.id:
                await doc_repo.update_current_version_id(fresh_doc, best_version.id)
                await session.commit()
                logger.info(
                    "INDEXING: updated document %s current_version_id → %s",
                    document.id, best_version.id,
                )

    await job_repo.mark_progress(job, progress=100, message="Version READY.")
    await session.commit()
    logger.info(
        "INDEXING complete: version=%s document=%s now READY",
        version.id, document.id,
    )


# Handler registry — later phases add real stage handlers here.
# NOTE: handle_comparison / the CONFLICT_SCAN handler / _run_summary_job /
# _run_extraction_job are NOT in this dict — those job types are dispatched
# directly in run_processing_job before the HANDLERS.get() lookup because
# their signatures differ (comparison/summary/extraction take their domain
# row; the org-wide scan takes no version/document at all) AND because their
# whole purpose is to run against an ALREADY-READY version — the pipeline
# path's READY-short-circuit would silently no-op them (plan §2.3/§5.4).
HANDLERS: dict[JobType, JobHandler] = {
    JobType.EXTRACTION: handle_extraction,
    JobType.CHUNKING: handle_chunking,
    JobType.EMBEDDING: handle_embedding,
    JobType.INDEXING: handle_indexing,
}


# ── COMPARISON stage handler (Phase 12 — comparison pipeline) ─────────────────

async def _run_comparison_job(
    ctx: dict[str, Any],
    job: ProcessingJob,
    session: AsyncSession,
) -> str:
    """Entry point for COMPARISON jobs — called directly from run_processing_job.

    Loads the DocumentComparison by job.comparison_id, validates tenancy,
    transitions status to PROCESSING, runs the full comparison pipeline, and
    marks COMPLETED or FAILED.
    """
    from app.models.comparison import DocumentComparison
    from app.repositories.document_comparison_repository import DocumentComparisonRepository
    from sqlalchemy import select

    job_repo = ProcessingJobRepository(session)
    comp_repo = DocumentComparisonRepository(session)

    # Load the comparison record
    comp_result = await session.execute(
        select(DocumentComparison).where(DocumentComparison.id == job.comparison_id)
    )
    comparison = comp_result.scalar_one_or_none()
    comparison_id_str = comparison.id if comparison is not None else None
    if comparison is None:
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message=f"DocumentComparison {job.comparison_id} not found.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    # Tenancy check
    if comparison.organization_id != job.organization_id:
        logger.error(
            "TENANCY MISMATCH on comparison job %s: payload org=%s, comparison org=%s",
            job.id, job.organization_id, comparison.organization_id,
        )
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message="Comparison job payload failed tenant validation.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    # Transition comparison to PROCESSING
    async with _atomic(session):
        await comp_repo.update_status(comparison, "PROCESSING")

    # Capture plain retry-budget values BEFORE the pipeline runs: any
    # mid-pipeline rollback expires the ORM instances, and reading ANY
    # attribute (even the PK) on an expired instance outside the async
    # greenlet raises MissingGreenlet.
    job_id_str = job.id
    attempts = job.attempts
    max_attempts = job.max_attempts

    try:
        await handle_comparison(ctx, job, comparison, session)
    except DeterministicJobError as exc:
        async with _atomic(session):
            await job_repo.mark_failed(job, error_message=exc.message, now=utc_now())
            await comp_repo.update_status(
                comparison, "FAILED", error_message=exc.message
            )
        await _dead_letter(job, exc.message)
        return JobStatus.FAILED.value
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("Comparison job %s failed (attempt %s/%s): %s",
                       job_id_str, attempts, max_attempts, message)
        if attempts < max_attempts:
            delay = get_backoff_seconds(attempts)
            # The failed pipeline's rollback expired the ORM instances —
            # reload BOTH fresh before the RETRYING transition writes.
            fresh_job = await job_repo.get_by_id(job_id_str)
            fresh_comparison = await comp_repo.get_by_id(comparison_id_str)
            if fresh_job is None or fresh_comparison is None:
                logger.error(
                    "Comparison job %s vanished mid-retry — aborting", job_id_str
                )
                return JobStatus.FAILED.value
            async with _atomic(session):
                await ProcessingJobRepository(session).mark_retrying(
                    fresh_job, error_message=message, now=utc_now()
                )
                await comp_repo.update_status(fresh_comparison, "PENDING")
            try:
                await enqueue_processing_job(job_id_str, delay_seconds=delay)
            except Exception:
                logger.exception(
                    "Re-enqueue failed for RETRYING comparison job %s — sweep will recover", job_id_str
                )
            return JobStatus.RETRYING.value
        # Retries exhausted
        fresh_job = await job_repo.get_by_id(job_id_str)
        fresh_comparison = await comp_repo.get_by_id(comparison_id_str)
        if fresh_job is None or fresh_comparison is None:
            logger.error(
                "Comparison job %s vanished at retry exhaustion — aborting", job_id_str
            )
            return JobStatus.FAILED.value
        async with _atomic(session):
            await ProcessingJobRepository(session).mark_failed(
                fresh_job,
                error_message=f"Retries exhausted. Last error: {message}",
                now=utc_now(),
            )
            await comp_repo.update_status(
                fresh_comparison, "FAILED",
                error_message=f"Retries exhausted. Last error: {message}"
            )
        await _dead_letter(fresh_job, message)
        return JobStatus.FAILED.value

    # Success
    async with _atomic(session):
        await job_repo.mark_completed(job, now=utc_now())
    logger.info(
        "Comparison job %s COMPLETED: comparison=%s",
        job.id, comparison.id,
    )
    return JobStatus.COMPLETED.value


# ── SUMMARY stage handler (Phase 14 — summary pipeline) ───────────────────────

async def _run_summary_job(
    ctx: dict[str, Any],
    job: ProcessingJob,
    session: AsyncSession,
) -> str:
    """Entry point for SUMMARY jobs — called directly from run_processing_job.

    Mirrors _run_comparison_job exactly in shape: loads the DocumentSummary
    by job.summary_id, validates tenancy, transitions the row to PROCESSING,
    calls the thin service handler, marks COMPLETED/FAILED on the DOMAIN ROW
    (never document_versions.status).  The pipeline runs against an
    already-READY version — the generic path's READY-short-circuit would
    silently no-op it (plan §2.3).
    """
    from app.models.summary import DocumentSummary
    from app.repositories.document_summary_repository import DocumentSummaryRepository
    from app.services.summary_service import SummaryService
    from sqlalchemy import select

    job_repo = ProcessingJobRepository(session)
    summary_repo = DocumentSummaryRepository(session)

    # Load the summary record
    summary_result = await session.execute(
        select(DocumentSummary).where(DocumentSummary.id == job.summary_id)
    )
    summary = summary_result.scalar_one_or_none()
    summary_id_str = summary.id if summary is not None else None
    if summary is None:
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message=f"DocumentSummary {job.summary_id} not found.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    # Tenancy check
    if summary.organization_id != job.organization_id:
        logger.error(
            "TENANCY MISMATCH on summary job %s: payload org=%s, summary org=%s",
            job.id, job.organization_id, summary.organization_id,
        )
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message="Summary job payload failed tenant validation.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    # Capture plain retry-budget values BEFORE the pipeline runs (the failed
    # pipeline's rollback expires the ORM instances — same rationale as the
    # comparison job's comment).
    job_id_str = job.id
    attempts = job.attempts
    max_attempts = job.max_attempts

    try:
        await SummaryService.run(job, summary, session)
    except DeterministicJobError as exc:
        await _fail_special_job(
            session, job_repo, summary_repo, "summary",
            job_id_str, summary_id_str, exc.message,
        )
        return JobStatus.FAILED.value
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Summary job %s failed (attempt %s/%s): %s",
            job_id_str, attempts, max_attempts, message,
        )
        if attempts < max_attempts:
            delay = get_backoff_seconds(attempts)
            fresh_job = await job_repo.get_by_id(job_id_str)
            fresh_summary = await summary_repo.get_by_id(summary_id_str or "")
            if fresh_job is None or fresh_summary is None:
                logger.error(
                    "Summary job %s vanished mid-retry — aborting", job_id_str
                )
                return JobStatus.FAILED.value
            async with _atomic(session):
                await ProcessingJobRepository(session).mark_retrying(
                    fresh_job, error_message=message, now=utc_now()
                )
                await summary_repo.update_status(fresh_summary, "PENDING")
            try:
                await enqueue_processing_job(job_id_str, delay_seconds=delay)
            except Exception:
                logger.exception(
                    "Re-enqueue failed for RETRYING summary job %s — sweep will recover",
                    job_id_str,
                )
            return JobStatus.RETRYING.value
        # Retries exhausted
        await _fail_special_job(
            session, job_repo, summary_repo, "summary",
            job_id_str, summary_id_str,
            f"Retries exhausted. Last error: {message}",
        )
        return JobStatus.FAILED.value

    # Success
    async with _atomic(session):
        await job_repo.mark_completed(job, now=utc_now())
    logger.info("Summary job %s COMPLETED: summary=%s", job.id, summary.id)
    return JobStatus.COMPLETED.value


# ── STRUCTURED_EXTRACTION stage handler (Phase 14 — extraction pipeline) ──────

async def _run_extraction_job(
    ctx: dict[str, Any],
    job: ProcessingJob,
    session: AsyncSession,
) -> str:
    """Entry point for STRUCTURED_EXTRACTION jobs — mirrors _run_summary_job.

    Deliberately a DIFFERENT job type from the Phase 5 EXTRACTION ingestion
    stage (plan §2.4's name-collision correction).
    """
    from app.models.extraction import DocumentExtraction
    from app.repositories.document_extraction_repository import (
        DocumentExtractionRepository,
    )
    from app.services.extraction_service import ExtractionService
    from sqlalchemy import select

    job_repo = ProcessingJobRepository(session)
    extraction_repo = DocumentExtractionRepository(session)

    extraction_result = await session.execute(
        select(DocumentExtraction).where(DocumentExtraction.id == job.extraction_id)
    )
    extraction = extraction_result.scalar_one_or_none()
    extraction_id_str = extraction.id if extraction is not None else None
    if extraction is None:
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message=f"DocumentExtraction {job.extraction_id} not found.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    if extraction.organization_id != job.organization_id:
        logger.error(
            "TENANCY MISMATCH on extraction job %s: payload org=%s, extraction org=%s",
            job.id, job.organization_id, extraction.organization_id,
        )
        async with _atomic(session):
            await job_repo.mark_failed(
                job,
                error_message="Extraction job payload failed tenant validation.",
                now=utc_now(),
            )
        return JobStatus.FAILED.value

    job_id_str = job.id
    attempts = job.attempts
    max_attempts = job.max_attempts

    try:
        await ExtractionService.run(job, extraction, session)
    except DeterministicJobError as exc:
        await _fail_special_job(
            session, job_repo, extraction_repo, "extraction",
            job_id_str, extraction_id_str, exc.message,
        )
        return JobStatus.FAILED.value
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Extraction job %s failed (attempt %s/%s): %s",
            job_id_str, attempts, max_attempts, message,
        )
        if attempts < max_attempts:
            delay = get_backoff_seconds(attempts)
            fresh_job = await job_repo.get_by_id(job_id_str)
            fresh_run = await extraction_repo.get_by_id(extraction_id_str or "")
            if fresh_job is None or fresh_run is None:
                logger.error(
                    "Extraction job %s vanished mid-retry — aborting", job_id_str
                )
                return JobStatus.FAILED.value
            async with _atomic(session):
                await ProcessingJobRepository(session).mark_retrying(
                    fresh_job, error_message=message, now=utc_now()
                )
                await extraction_repo.update_status(fresh_run, "PENDING")
            try:
                await enqueue_processing_job(job_id_str, delay_seconds=delay)
            except Exception:
                logger.exception(
                    "Re-enqueue failed for RETRYING extraction job %s — sweep will recover",
                    job_id_str,
                )
            return JobStatus.RETRYING.value
        await _fail_special_job(
            session, job_repo, extraction_repo, "extraction",
            job_id_str, extraction_id_str,
            f"Retries exhausted. Last error: {message}",
        )
        return JobStatus.FAILED.value

    async with _atomic(session):
        await job_repo.mark_completed(job, now=utc_now())
    logger.info("Extraction job %s COMPLETED: run=%s", job.id, extraction.id)
    return JobStatus.COMPLETED.value


async def _fail_special_job(
    session: AsyncSession,
    job_repo: ProcessingJobRepository,
    domain_repo: Any,
    domain_name: str,
    job_id: str,
    domain_id: str | None,
    message: str,
) -> None:
    """Terminal FAILED for a special-dispatch job + its domain row + dead-letter.

    Shared by the summary/extraction paths — reloads both rows fresh (the
    failed pipeline's rollback may have expired the ORM instances), mirrors
    the comparison path's exhaustion block.
    """
    fresh_job = await job_repo.get_by_id(job_id)
    fresh_domain = await domain_repo.get_by_id(domain_id or "") if domain_id else None
    if fresh_job is None or fresh_domain is None:
        logger.error(
            "%s job %s vanished at retry exhaustion — aborting",
            domain_name, job_id,
        )
        return
    async with _atomic(session):
        await ProcessingJobRepository(session).mark_failed(
            fresh_job, error_message=message, now=utc_now()
        )
        await domain_repo.update_status(
            fresh_domain, "FAILED", error_message=message
        )
    await _dead_letter(fresh_job, message)


async def handle_comparison(
    ctx: dict[str, Any],
    job: ProcessingJob,
    comparison: object,
    session: AsyncSession,
) -> None:
    """Full comparison pipeline: alignment → text diff → semantic → classify → persist.

    Implements §9.4–§9.9 of the Phase 12 plan:
      1. Load both versions' sections and chunks.
      2. Section alignment (number match → title match → embedding similarity).
      3. Per-aligned-pair: text diff (deterministic, §9.5).
      4. Per-MODIFIED pair: semantic materiality call (LLM, bounded, §9.6).
      5. Severity classification (pure, §9.8).
      6. Persist comparison_changes INCREMENTALLY (one INSERT + commit per section).
      7. On ADDED/REMOVED sections: persist immediately as MODERATE/MAJOR.
      8. Update job progress after each section.
      9. On completion: persist summary counts and mark COMPLETED.

    Resumability: already-persisted sections (by section label) are skipped
    on retry so changes are never duplicated (Backend §49).
    """
    from app.domain.comparison_rules import (
        classify_severity,
        compute_proportion_changed,
        is_critical_section as check_critical,
    )
    from app.domain.section_alignment import align_sections
    from app.domain.text_diff import diff_text, proportion_from_diff
    from app.models.document import DocumentSection
    from app.models.organization import Organization
    from app.rag.comparison_narration import classify_section_semantically
    from app.repositories.document_comparison_repository import DocumentComparisonRepository
    from app.repositories.document_section_repository import DocumentSectionRepository
    from app.repositories.document_chunk_repository import DocumentChunkRepository
    from sqlalchemy import select

    job_repo = ProcessingJobRepository(session)
    comp_repo = DocumentComparisonRepository(session)
    section_repo = DocumentSectionRepository(session)
    chunk_repo = DocumentChunkRepository(session)

    comparison_id = comparison.id  # type: ignore[attr-defined]
    version_a_id = comparison.document_a_version_id  # type: ignore[attr-defined]
    version_b_id = comparison.document_b_version_id  # type: ignore[attr-defined]
    org_id = comparison.organization_id  # type: ignore[attr-defined]

    await job_repo.mark_progress(job, progress=2, message="Loading sections…")

    # 1. Load sections for both versions
    sections_a = await section_repo.list_for_version(version_a_id)
    sections_b = await section_repo.list_for_version(version_b_id)

    # 2. Load critical_sections config from org settings
    org_result = await session.execute(
        select(Organization).where(Organization.id == org_id)
    )
    org = org_result.scalar_one_or_none()
    critical_patterns: list[str] = []
    if org is not None:
        try:
            comp_settings = (org.settings or {}).get("comparison", {})
            critical_patterns = comp_settings.get("critical_sections", [])
        except Exception:  # noqa: BLE001
            pass

    # 3. Align sections
    await job_repo.mark_progress(job, progress=5, message="Aligning sections…")
    alignments, alignment_degraded = align_sections(
        sections_a, sections_b, embedding_lookup=None  # embeddings skipped in V1
    )

    total_sections = len(alignments)
    if total_sections == 0:
        # No sections at all — mark completed with empty summary
        async with _atomic(session):
            await comp_repo.update_status(
                comparison, "COMPLETED",
                summary={"total": 0, "major": 0, "moderate": 0, "minor": 0,
                         "alignment_degraded": alignment_degraded},
            )
        return

    # 4. Skip already-persisted sections (resumability)
    done_sections = await comp_repo.list_done_sections(comparison_id)

    # 5. Resolve LLM provider for semantic calls (may be None — graceful degradation)
    try:
        from app.infrastructure.llm import get_llm_provider
        llm_provider = get_llm_provider()
    except Exception:  # noqa: BLE001
        llm_provider = None

    await job_repo.mark_progress(job, progress=10, message="Comparing sections…")

    section_idx = 0
    for alignment in alignments:
        section_idx += 1
        sec_a = alignment.section_a
        sec_b = alignment.section_b

        # Section label for dedup / resume — MUST be byte-identical to the
        # value persisted in comparison_changes.section below (title-first),
        # or the done_sections resume check never matches (Task 8 point 4).
        if alignment.match_method == "whole_document":
            section_label = "(whole document)"
        else:
            sec = sec_a or sec_b
            section_label = (
                getattr(sec, "title", None)
                or getattr(sec, "section_number", None)
                or f"section_{section_idx}"
            )

        if section_label in done_sections:
            continue  # already persisted on a previous attempt

        # Progress update
        progress = 10 + int(80 * section_idx / total_sections)
        await job_repo.mark_progress(
            job, progress=progress,
            message=f"Comparing section {section_idx}/{total_sections}…"
        )

        # Handle whole-document fallback
        if alignment.match_method == "whole_document":
            await _compare_whole_document(
                session, comp_repo, comparison, job,
                version_a_id, version_b_id,
                critical_patterns, llm_provider,
                section_label,
            )
            await session.commit()
            continue

        # ADDED: section exists only in B
        if sec_a is None and sec_b is not None:
            is_crit = check_critical(
                getattr(sec_b, "title", None),
                getattr(sec_b, "section_number", None),
                critical_patterns,
            )
            severity = classify_severity(
                change_type="ADDED",
                semantic_materiality=None,
                is_critical_section=is_crit,
                proportion_changed=1.0,
            )
            # Get representative chunk for new_chunk_id
            new_chunks = await chunk_repo.list_for_section(
                getattr(sec_b, "id", None) or ""
            ) if hasattr(chunk_repo, "list_for_section") else []
            new_chunk_id = new_chunks[0].id if new_chunks else None
            new_text = " ".join(c.content for c in new_chunks[:3]) if new_chunks else None
            async with _atomic(session):
                await comp_repo.add_change(
                    comparison_id,
                    change_type="ADDED",
                    severity=severity,
                    section=section_label,
                    new_chunk_id=new_chunk_id,
                    new_text=new_text,
                )
            continue

        # REMOVED: section exists only in A
        if sec_b is None and sec_a is not None:
            is_crit = check_critical(
                getattr(sec_a, "title", None),
                getattr(sec_a, "section_number", None),
                critical_patterns,
            )
            severity = classify_severity(
                change_type="REMOVED",
                semantic_materiality=None,
                is_critical_section=is_crit,
                proportion_changed=1.0,
            )
            old_chunks = await chunk_repo.list_for_section(
                getattr(sec_a, "id", None) or ""
            ) if hasattr(chunk_repo, "list_for_section") else []
            old_chunk_id = old_chunks[0].id if old_chunks else None
            old_text = " ".join(c.content for c in old_chunks[:3]) if old_chunks else None
            async with _atomic(session):
                await comp_repo.add_change(
                    comparison_id,
                    change_type="REMOVED",
                    severity=severity,
                    section=section_label,
                    old_chunk_id=old_chunk_id,
                    old_text=old_text,
                )
            continue

        # MODIFIED: both sides present — run text diff
        if sec_a is None or sec_b is None:
            continue  # shouldn't happen but guard defensively

        is_crit = check_critical(
            getattr(sec_a, "title", None),
            getattr(sec_a, "section_number", None),
            critical_patterns,
        )

        # Get chunk content for both sides
        old_chunks = await chunk_repo.list_for_section(
            getattr(sec_a, "id", None) or ""
        ) if hasattr(chunk_repo, "list_for_section") else []
        new_chunks = await chunk_repo.list_for_section(
            getattr(sec_b, "id", None) or ""
        ) if hasattr(chunk_repo, "list_for_section") else []

        old_text_full = " ".join(getattr(c, "content", "") for c in old_chunks)
        new_text_full = " ".join(getattr(c, "content", "") for c in new_chunks)

        # Content-hash short-circuit (§9.5 step 1): if chunk hashes are all identical, skip
        old_hashes = [getattr(c, "content_hash", None) for c in old_chunks]
        new_hashes = [getattr(c, "content_hash", None) for c in new_chunks]
        if old_hashes and old_hashes == new_hashes:
            # UNCHANGED — not persisted
            continue

        # §9.5: full text diff
        diff_result = diff_text(old_text_full, new_text_full)
        if diff_result.is_unchanged:
            continue  # formatting-only — absorbed as UNCHANGED

        proportion = proportion_from_diff(diff_result)

        # §9.6: semantic materiality call (LLM, bounded, graceful)
        materiality: str | None = None
        truncated = False
        if llm_provider is not None:
            try:
                materiality, truncated = await classify_section_semantically(
                    old_text_full,
                    new_text_full,
                    provider=llm_provider,
                )
            except Exception:  # noqa: BLE001
                materiality = None
                truncated = False

        # §9.8: severity classification
        severity = classify_severity(
            change_type="MODIFIED",
            semantic_materiality=materiality,  # type: ignore[arg-type]
            is_critical_section=is_crit,
            proportion_changed=proportion,
        )

        old_chunk_id = old_chunks[0].id if old_chunks else None
        new_chunk_id = new_chunks[0].id if new_chunks else None

        async with _atomic(session):
            await comp_repo.add_change(
                comparison_id,
                change_type="MODIFIED",
                severity=severity,
                section=section_label,
                old_chunk_id=old_chunk_id,
                new_chunk_id=new_chunk_id,
                old_text=old_text_full[:2000] if old_text_full else None,
                new_text=new_text_full[:2000] if new_text_full else None,
                truncated=truncated,
            )

    # 6. Build summary and mark COMPLETED
    await job_repo.mark_progress(job, progress=98, message="Finalising…")
    counts = await comp_repo.count_by_severity(comparison_id)
    total = sum(counts.values())
    summary = {
        "total": total,
        "major": counts.get("MAJOR", 0),
        "moderate": counts.get("MODERATE", 0),
        "minor": counts.get("MINOR", 0),
        "alignment_degraded": alignment_degraded,
    }
    async with _atomic(session):
        await comp_repo.update_status(
            comparison, "COMPLETED", summary=summary
        )

    # ── Phase 13: comparison-derived conflict seeding (§13) ─────────────────
    # The single, minimal touch-point into Phase-12-owned code: immediately
    # after status = COMPLETED, qualifying changes (MODIFIED + MAJOR + both
    # sides CURRENT) seed conflicts through the same dedup/persistence path
    # the background scan uses.  Deterministic — no LLM call in this path.
    try:
        from app.services.conflict_service import ConflictService

        await ConflictService.seed_from_comparison(comparison_id, session)
    except Exception:  # noqa: BLE001 — seeding must never fail the comparison
        logger.exception(
            "Conflict seeding failed for comparison %s (non-fatal)", comparison_id
        )

    logger.info(
        "handle_comparison: comparison=%s total_changes=%d major=%d moderate=%d minor=%d",
        comparison_id, total, counts.get("MAJOR", 0),
        counts.get("MODERATE", 0), counts.get("MINOR", 0),
    )


async def _compare_whole_document(
    session: AsyncSession,
    comp_repo: object,
    comparison: object,
    job: ProcessingJob,
    version_a_id: str,
    version_b_id: str,
    critical_patterns: list[str],
    llm_provider: object | None,
    section_label: str,
) -> None:
    """Whole-document fallback comparison when section alignment is degraded (§9.4)."""
    from app.domain.comparison_rules import classify_severity
    from app.domain.text_diff import diff_text, proportion_from_diff
    from app.rag.comparison_narration import classify_section_semantically
    from app.repositories.document_chunk_repository import DocumentChunkRepository

    chunk_repo = DocumentChunkRepository(session)

    # Load all chunks (no section filter — whole document)
    old_chunks = await chunk_repo.list_for_version_text(version_a_id)
    new_chunks = await chunk_repo.list_for_version_text(version_b_id)

    old_text = " ".join(getattr(c, "content", "") for c in old_chunks)
    new_text = " ".join(getattr(c, "content", "") for c in new_chunks)

    diff_result = diff_text(old_text, new_text)
    if diff_result.is_unchanged:
        return

    proportion = proportion_from_diff(diff_result)
    materiality = None
    truncated = False
    if llm_provider is not None:
        try:
            materiality, truncated = await classify_section_semantically(
                old_text, new_text, provider=llm_provider  # type: ignore[arg-type]
            )
        except Exception:  # noqa: BLE001
            pass

    severity = classify_severity(
        change_type="MODIFIED",
        semantic_materiality=materiality,  # type: ignore[arg-type]
        is_critical_section=False,
        proportion_changed=proportion,
    )
    await comp_repo.add_change(  # type: ignore[attr-defined]
        comparison.id,  # type: ignore[attr-defined]
        change_type="MODIFIED",
        severity=severity,
        section="(whole document)",
        old_text=old_text[:2000] if old_text else None,
        new_text=new_text[:2000] if new_text else None,
        truncated=truncated,
    )


# ── CONFLICT_SCAN handler (Phase 13 — org-wide background scan) ────────────────

async def _run_conflict_scan_job(
    ctx: dict[str, Any],
    job: ProcessingJob,
    session: AsyncSession,
) -> str:
    """Entry point for CONFLICT_SCAN jobs — called directly from run_processing_job.

    Mirrors the _run_comparison_job early-branch pattern: transitions to
    PROCESSING already happened at claim; this runs the checkpointed scan,
    then COMPLETED on success or the existing retry policy on transient
    error.  A CONFLICT_SCAN failure NEVER touches any DocumentVersion status
    (the job is org-wide — there is no version to fail).
    """
    from app.services.conflict_service import ConflictService

    job_repo = ProcessingJobRepository(session)

    # Capture plain retry-budget values BEFORE the pipeline runs (same
    # expired-ORM-instance discipline as the comparison path).
    job_id_str = job.id
    attempts = job.attempts
    max_attempts = job.max_attempts

    try:
        await ConflictService.run_scan(
            organization_id=job.organization_id, job=job, db=session
        )
    except DeterministicJobError as exc:
        # The scan loop raises OUTSIDE any _atomic block — normalize the
        # aborted transaction before the failure-path writes.
        await session.rollback()
        async with _atomic(session):
            await job_repo.mark_failed(job, error_message=exc.message, now=utc_now())
        await _dead_letter(job, exc.message)
        return JobStatus.FAILED.value
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        await session.rollback()
        logger.warning(
            "Conflict scan job %s failed (attempt %s/%s): %s",
            job_id_str, attempts, max_attempts, message,
        )
        if attempts < max_attempts:
            delay = get_backoff_seconds(attempts)
            fresh_job = await job_repo.get_by_id(job_id_str)
            if fresh_job is None:
                logger.error(
                    "Conflict scan job %s vanished mid-retry — aborting", job_id_str
                )
                return JobStatus.FAILED.value
            async with _atomic(session):
                await ProcessingJobRepository(session).mark_retrying(
                    fresh_job, error_message=message, now=utc_now()
                )
            try:
                await enqueue_processing_job(
                    job_id_str, queue_name=QUEUE_LOW, delay_seconds=delay
                )
            except Exception:
                logger.exception(
                    "Re-enqueue failed for RETRYING conflict scan %s — sweep will recover",
                    job_id_str,
                )
            return JobStatus.RETRYING.value
        # Retries exhausted
        fresh_job = await job_repo.get_by_id(job_id_str)
        if fresh_job is None:
            logger.error(
                "Conflict scan job %s vanished at retry exhaustion — aborting", job_id_str
            )
            return JobStatus.FAILED.value
        async with _atomic(session):
            await ProcessingJobRepository(session).mark_failed(
                fresh_job,
                error_message=f"Retries exhausted. Last error: {message}",
                now=utc_now(),
            )
        await _dead_letter(fresh_job, message)
        return JobStatus.FAILED.value

    # Success — the scan's checkpoint retains its final counters.
    async with _atomic(session):
        await job_repo.mark_completed(job, now=utc_now())
    logger.info("Conflict scan job %s COMPLETED", job.id)
    return JobStatus.COMPLETED.value


# ── Conflict-scan cron trigger (Phase 13 — §15) ───────────────────────────────

async def trigger_conflict_scans(ctx: dict[str, Any]) -> int:
    """Nightly cron entry: create+enqueue one CONFLICT_SCAN job per active org.

    For each active organization with no currently in-flight (PENDING /
    PROCESSING / RETRYING) CONFLICT_SCAN job, exactly one org-wide scan job
    is created (document_version_id NULL) and enqueued on the LOW queue —
    maintenance/housekeeping never starves ingestion bursts.  Returns the
    number of scans actually triggered.
    """
    from sqlalchemy import select

    from app.infrastructure.database import get_session_factory
    from app.models.organization import Organization
    from app.services.job_service import JobService

    session_factory = get_session_factory()
    triggered = 0
    async with session_factory() as session:
        orgs_result = await session.execute(
            select(Organization).order_by(Organization.id)
        )
        organizations = list(orgs_result.scalars().all())

        from app.repositories.conflict_repository import ConflictRepository

        conflict_repo = ConflictRepository(session)
        for organization in organizations:
            # Skip disabled orgs if a status column ever says so (defensive)
            if getattr(organization, "deleted_at", None) is not None:
                continue
            if await conflict_repo.has_in_flight_scan(organization.id):
                logger.debug(
                    "Conflict scan already in flight for org %s — skipping",
                    organization.id,
                )
                continue
            job = await JobService.create_for_org_scan(
                session, organization_id=organization.id
            )
            await session.commit()
            # Pointer AFTER commit (durable-row-then-pointer — Backend §50);
            # failures are logged, never raised — the sweep recovers.
            await JobService.enqueue_after_commit(job, queue_name=QUEUE_LOW)
            triggered += 1
            logger.info(
                "Conflict scan triggered for org %s (job %s)",
                organization.id, job.id,
            )

    logger.info("trigger_conflict_scans: %d scan job(s) triggered", triggered)
    return triggered


# ── The single Arq job function ───────────────────────────────────────────────

async def run_processing_job(ctx: dict[str, Any], job_id: str) -> str:
    """Execute one processing job end-to-end. Returns the final status.

    This function NEVER raises for expected failure modes (handler errors,
    tenancy mismatches, exhaustion) — those become terminal job states so the
    pipeline owns retry semantics. Truly unexpected errors (e.g. PostgreSQL
    unreachable during claim) may raise; Arq's own retry then re-runs the
    pointer safely (the claim/status guards make re-execution idempotent).
    """
    from app.infrastructure.database import get_session_factory

    session_factory = get_session_factory()
    async with session_factory() as session:
        job_repo = ProcessingJobRepository(session)

        # ── Load the authoritative record fresh ───────────────────────────
        job = await job_repo.get_by_id(job_id)
        if job is None:
            logger.warning("Job pointer %s has no processing_jobs row — skipping", job_id)
            return "NOT_FOUND"

        # Idempotency guard: terminal jobs are never re-executed
        if job.is_terminal:
            logger.info("Job %s already %s — skipping", job.id, job.status)
            return job.status

        now = utc_now()

        # ── Claim ─────────────────────────────────────────────────────────
        current = JobStatus(job.status)
        try:
            if current in (JobStatus.PENDING, JobStatus.RETRYING):
                assert_job_transition(current, JobStatus.PROCESSING)
                await job_repo.mark_processing(job, now=now)
            elif current is JobStatus.PROCESSING:
                # Stale re-execution after a worker crash — keep PROCESSING,
                # count the attempt (idempotent handler makes this safe).
                job.attempts += 1
                await session.flush()
            else:  # pragma: no cover — guarded by is_terminal above
                raise InvalidJobTransitionError(current, JobStatus.PROCESSING)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

        # ── Dispatch: COMPARISON jobs take a separate path ────────────────────
        if JobType(job.job_type) is JobType.COMPARISON:
            return await _run_comparison_job(ctx, job, session)

        # ── Dispatch: CONFLICT_SCAN jobs are org-wide (no version anchor) ─────
        if JobType(job.job_type) is JobType.CONFLICT_SCAN:
            return await _run_conflict_scan_job(ctx, job, session)

        # ── Dispatch: Phase 14 SUMMARY / STRUCTURED_EXTRACTION (special path) ──
        # CRITICAL (plan §2.3/§5.4): these must return BEFORE the version-
        # status logic below — their whole purpose is to run against an
        # already-READY version, which the per-version pipeline path
        # short-circuits to a silent moot success.
        if JobType(job.job_type) is JobType.SUMMARY:
            return await _run_summary_job(ctx, job, session)
        if JobType(job.job_type) is JobType.STRUCTURED_EXTRACTION:
            return await _run_extraction_job(ctx, job, session)

        # ── Load referenced entity FRESH + validate tenancy ───────────────
        ver_repo = DocumentVersionRepository(session)
        doc_repo = DocumentRepository(session)

        version = await ver_repo.get_by_id(job.document_version_id)
        if version is None:
            async with _atomic(session):
                await job_repo.mark_failed(
                    job,
                    error_message="Referenced document version no longer exists.",
                    now=utc_now(),
                )
            await _dead_letter(job, "Referenced document version no longer exists.")
            return JobStatus.FAILED.value

        document = await doc_repo.get_by_id(version.document_id)
        if document is None or document.organization_id != job.organization_id:
            # Tampered/malformed payload defense (Backend §14) — fail the job,
            # never touch a version that does not belong to the payload's org.
            logger.error(
                "TENANCY MISMATCH on job %s: payload org=%s, entity org=%s",
                job.id,
                job.organization_id,
                document.organization_id if document else None,
            )
            async with _atomic(session):
                await job_repo.mark_failed(
                    job,
                    error_message="Job payload failed tenant validation.",
                    now=utc_now(),
                )
            await _dead_letter(job, "Tenant validation failed.")
            return JobStatus.FAILED.value

        # ── Version status transition on claim ────────────────────────────
        v_cur = VersionStatus(version.status)
        if v_cur is VersionStatus.READY:
            # Pipeline already finished (stale pointer) — moot success
            async with _atomic(session):
                await job_repo.mark_completed(job, now=utc_now())
            return JobStatus.COMPLETED.value
        try:
            if v_cur is VersionStatus.UPLOADED or v_cur is VersionStatus.FAILED:
                assert_version_transition(v_cur, VersionStatus.PROCESSING)
                await ver_repo.update_status(version.id, VersionStatus.PROCESSING.value)
                await session.commit()
            # already PROCESSING / mid-pipeline → leave as-is
        except InvalidVersionTransitionError as exc:
            async with _atomic(session):
                await job_repo.mark_failed(
                    job, error_message=str(exc), now=utc_now()
                )
            await _dead_letter(job, str(exc))
            return JobStatus.FAILED.value

        # ── Dispatch to the handler ───────────────────────────────────────
        handler = HANDLERS.get(JobType(job.job_type))
        if handler is None:
            async with _atomic(session):
                await job_repo.mark_failed(
                    job,
                    error_message=f"No handler registered for job type {job.job_type}.",
                    now=utc_now(),
                )
            await _dead_letter(job, f"No handler for job type {job.job_type}.")
            return JobStatus.FAILED.value

        try:
            await handler(ctx, job, version, document, session)
        except DeterministicJobError as exc:
            async with _atomic(session):
                await job_repo.mark_failed(job, error_message=exc.message, now=utc_now())
                await _fail_version(ver_repo, version.id, exc.message)
            await _dead_letter(job, exc.message)
            return JobStatus.FAILED.value
        except Exception as exc:
            return await _handle_failure(session, job, ver_repo, version.id, exc)

        # ── Success ───────────────────────────────────────────────────────
        async with _atomic(session):
            await job_repo.mark_completed(job, now=utc_now())
        logger.info(
            "Job %s (%s) COMPLETED for version %s",
            job.id,
            job.job_type,
            version.id,
        )
        return JobStatus.COMPLETED.value


async def _handle_failure(
    session: AsyncSession,
    job: ProcessingJob,
    ver_repo: DocumentVersionRepository,
    version_id: str,
    exc: Exception,
) -> str:
    """Apply the retry policy to a handler failure.

    While the retry budget lasts: RETRYING (a distinct, visible state —
    Backend §47) + re-enqueue with exponential backoff. On exhaustion:
    terminal FAILED in PostgreSQL + version FAILED + dead-letter.
    """
    message = f"{type(exc).__name__}: {exc}"
    logger.warning("Job %s failed (attempt %s/%s): %s",
                   job.id, job.attempts, job.max_attempts, message)

    try:
        if job.attempts < job.max_attempts:
            delay = get_backoff_seconds(job.attempts)
            async with _atomic(session):
                await ProcessingJobRepository(session).mark_retrying(
                    job, error_message=message, now=utc_now()
                )
            # Pointer AFTER the RETRYING commit — sweep also covers this gap
            try:
                await enqueue_processing_job(job.id, delay_seconds=delay)
            except Exception:
                logger.exception(
                    "Re-enqueue failed for RETRYING job %s — sweep will recover", job.id
                )
            return JobStatus.RETRYING.value

        # Retries exhausted → terminal failure
        async with _atomic(session):
            await ProcessingJobRepository(session).mark_failed(
                job, error_message=f"Retries exhausted. Last error: {message}",
                now=utc_now(),
            )
            await _fail_version(ver_repo, version_id, message)
        await _dead_letter(job, message)
        logger.error(
            "Job %s (%s) FAILED permanently after %s attempts",
            job.id, job.job_type, job.attempts,
        )
        return JobStatus.FAILED.value
    except Exception:
        # The failure-path itself broke (e.g. DB down) — surface to Arq so the
        # pointer is retried later; PostgreSQL state remains recoverable.
        logger.exception("Failure handling errored for job %s", job.id)
        raise


async def _fail_version(
    ver_repo: DocumentVersionRepository, version_id: str, error_message: str
) -> None:
    """Transition the version to FAILED (terminal until an explicit retry)."""
    version = await ver_repo.get_by_id(version_id)
    if version is None:
        return
    v_cur = VersionStatus(version.status)
    if v_cur is VersionStatus.READY:
        return  # terminal success is never overwritten
    try:
        assert_version_transition(v_cur, VersionStatus.FAILED)
    except InvalidVersionTransitionError:
        return  # already FAILED
    await ver_repo.update_status(version_id, VersionStatus.FAILED.value,
                                 error_message=error_message)


async def _dead_letter(job: ProcessingJob, error_message: str) -> None:
    """Best-effort ops visibility — metadata only, never document content."""
    await push_dead_letter(
        {
            "job_id": job.id,
            "job_type": job.job_type,
            "organization_id": job.organization_id,
            "document_version_id": job.document_version_id,
            "attempts": job.attempts,
            "error": error_message[:500],
            "failed_at": utc_now().isoformat(),
        }
    )


# ── Reconciliation sweep ──────────────────────────────────────────────────────

async def reconciliation_sweep(ctx: dict[str, Any]) -> int:
    """Re-enqueue PostgreSQL jobs whose Redis pointers are missing (Backend §23).

    Runs on worker startup and periodically (Arq cron). This is what makes
    "Redis flushed" a non-event: every PENDING/RETRYING/stuck-PROCESSING row
    gets a fresh pointer; live pointers are deduplicated by job id.

    Returns the number of pointers actually (re-)enqueued.
    """
    from app.infrastructure.database import get_session_factory

    settings = get_settings()
    now = utc_now()
    pending_before = now - timedelta(
        seconds=settings.reconciliation_sweep_interval_seconds * 2
    )
    processing_before = now - timedelta(seconds=settings.job_stuck_threshold_seconds)

    session_factory = get_session_factory()
    async with session_factory() as session:
        candidates = await ProcessingJobRepository(session).list_jobs_for_sweep(
            pending_before=pending_before,
            processing_before=processing_before,
        )

    requeued = 0
    for job in candidates:
        try:
            if await enqueue_processing_job(job.id):
                requeued += 1
                logger.info(
                    "Sweep re-enqueued job %s (%s, status=%s)",
                    job.id, job.job_type, job.status,
                )
        except Exception:
            logger.exception("Sweep could not re-enqueue job %s", job.id)
            break  # Redis is down — stop hammering, next sweep retries

    if candidates:
        logger.info(
            "Reconciliation sweep: %d candidate(s), %d re-enqueued",
            len(candidates), requeued,
        )
    return requeued
