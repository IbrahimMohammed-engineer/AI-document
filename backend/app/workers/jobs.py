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
from app.infrastructure.queue import enqueue_processing_job, push_dead_letter
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
HANDLERS: dict[JobType, JobHandler] = {
    JobType.EXTRACTION: handle_extraction,
    JobType.CHUNKING: handle_chunking,
    JobType.EMBEDDING: handle_embedding,
    JobType.INDEXING: handle_indexing,
}


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
