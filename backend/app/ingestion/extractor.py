"""
Extraction stage orchestrator (roadmap Phase 5; Backend §18/§19).

Consumes the parser + OCR abstractions and produces `document_pages` rows:

    download → re-verify magic bytes → select parser by DETECTED type
      → per-page loop: native text → per-page needs-OCR classification
        → (rasterize 300 DPI → OCRProvider.recognize with client retries
           → on final failure: explicit "OCR failed" marker, page continues)
      → batched incremental persistence (committed every N pages so a crash
        resumes from the last persisted page — never blind re-INSERT)
      → page_count backfilled on the version row

Status transitions owned here:
    PROCESSING → EXTRACTING  at stage start
    EXTRACTING → OCR         when the first page is routed to OCR
                             (skipped entirely for fully text-native files —
                             never transitioned-through, Backend §47)

Idempotency (Backend §49): pages already persisted for the version are
skipped — resume starts at max(persisted page_number) + 1; inserts are
ON CONFLICT DO NOTHING, so overlapping re-runs never duplicate.

Error taxonomy: parser ExtractionErrors are DETERMINISTIC (terminal FAILED);
transient storage/provider faults propagate to the worker's generic retry
policy (RETRYING with backoff per Phase 4).

No file-type branching outside ingestion/parser.py — adding a format means
adding one parser, not touching this orchestrator (Backend §18).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.documents import VersionStatus
from app.domain.state_machines import assert_version_transition
from app.infrastructure.ocr import (
    OCRPageError,
    OCRProvider,
    OCRProviderUnavailable,
    PageContext,
    get_ocr_provider,
    recognize_with_retry,
)
from app.ingestion.parser import (
    ExtractionError,
    ParsedPage,
    UnsupportedFileTypeError,
    detect_content_type,
    needs_ocr,
    parser_for_mime,
)
from app.models.document import Document, DocumentVersion
from app.infrastructure.storage import (
    ObjectStorageProvider,
    StorageObjectMissingError,
    get_storage_provider,
)
from app.repositories.document_page_repository import DocumentPageRepository
from app.repositories.document_repository import DocumentVersionRepository
from app.repositories.processing_job_repository import (
    ProcessingJob,
    ProcessingJobRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class ExtractionOutcome:
    """Summary of one extraction run (observability + tests)."""

    page_count: int = 0
    pages_persisted: int = 0
    pages_skipped: int = 0
    ocr_pages: int = 0
    ocr_failed_pages: int = 0
    detected_mime: str = ""


async def run_extraction(
    session: AsyncSession,
    *,
    version: DocumentVersion,
    document: Document,
    job: ProcessingJob,
    job_repo: ProcessingJobRepository,
    ver_repo: DocumentVersionRepository,
    storage: ObjectStorageProvider | None = None,
    ocr_provider: OCRProvider | None = None,
) -> ExtractionOutcome:
    """Run the full EXTRACTION stage for one document version.

    The handler wrapper (workers/jobs.py) translates ExtractionError into
    DeterministicJobError; transient faults propagate for the Phase 4 retry
    policy. Page batches are committed HERE so crashes resume.
    """
    settings = get_settings()
    page_repo = DocumentPageRepository(session)
    storage = storage or get_storage_provider()

    # ── Version status: PROCESSING → EXTRACTING (idempotent on resume) ────
    await _ensure_extracting(session, ver_repo, version)

    await job_repo.mark_progress(job, progress=2, message="Downloading file…")

    # ── Download the durable bytes ─────────────────────────────────────────
    try:
        data = await storage.download(version.storage_key)
    except StorageObjectMissingError as exc:
        # Deterministic — the upload's bytes are gone; retrying cannot help
        raise ExtractionError(
            "Uploaded file object is missing from storage. "
            "Re-upload the document.",
            code="STORAGE_OBJECT_MISSING",
        ) from exc

    # ── Re-verify the content type from magic bytes (defense-in-depth —
    #    upload-time validation is not trusted twice, Phase 5 security note)
    detected = detect_content_type(data)
    if detected is None:
        raise UnsupportedFileTypeError(
            "File content could not be identified — it is not a readable "
            "PDF or DOCX."
        )
    if detected != version.mime_type:
        logger.warning(
            "Content-type mismatch for version %s (declared=%s, detected=%s) "
            "— parsing by detected type",
            version.id, version.mime_type, detected,
        )
    parser = parser_for_mime(detected, ocr_dpi=settings.ocr_dpi)

    outcome = ExtractionOutcome(detected_mime=detected)

    # ── Idempotent resume anchor: skip pages persisted by a prior attempt ──
    resume_page = await page_repo.get_max_page_number(version.id) + 1
    if resume_page > 1:
        logger.info(
            "Resuming extraction for version %s from page %d "
            "(%d page(s) already persisted)",
            version.id, resume_page, resume_page - 1,
        )

    ocr_cell = _OCRCell(ocr_provider)  # provider built lazily on first OCR page
    batch: list[dict] = []
    pages_done = 0
    batch_size = max(1, settings.extraction_page_batch_size)

    # parser.open() already raises the typed deterministic errors eagerly
    pages = parser.open(data)
    try:
        total = parser.total_pages or 0
        while True:
            # Parser iteration is the ONLY thing wrapped as deterministic:
            # a crash pulling the next page is this untrusted file's
            # fault (terminal FILE_CORRUPT). Loop-body faults (DB,
            # storage, OCR orchestration) propagate raw → the worker's
            # generic retry policy (RETRYING + resume, Backend §49).
            try:
                parsed = next(pages)
            except StopIteration:
                break
            except Exception as exc:
                raise ExtractionError(
                    f"The file could not be fully parsed: {exc}",
                    code="FILE_CORRUPT",
                ) from exc

            pages_done += 1
            if parsed.page_number < resume_page:
                outcome.pages_skipped += 1
                continue

            ocr_routed = (
                needs_ocr(
                    parsed.text,
                    min_alnum_chars=settings.ocr_min_page_alnum_chars,
                )
                and parsed.rasterize is not None
            )
            meta = dict(parsed.metadata or {})
            text = parsed.text
            ocr_failed = False

            if ocr_routed:
                await _enter_ocr_stage(session, ver_repo, version)
                if ocr_cell.provider is None:
                    ocr_cell.provider = get_ocr_provider()
                text, meta, ocr_failed = await _recognize_page(
                    parsed,
                    version=version,
                    provider=ocr_cell.provider,
                    settings=settings,
                    base_metadata=meta,
                )

            outcome.ocr_pages += 1 if ocr_routed else 0
            outcome.ocr_failed_pages += 1 if ocr_failed else 0

            batch.append(
                {
                    "document_version_id": version.id,
                    "page_number": parsed.page_number,
                    "text": text,
                    "ocr_used": ocr_routed,
                    "width": parsed.width,
                    "height": parsed.height,
                    "metadata": meta,
                }
            )

            if len(batch) >= batch_size:
                await page_repo.insert_pages(batch)
                await session.commit()  # durability checkpoint — resume anchor
                outcome.pages_persisted += len(batch)
                batch.clear()

            if total:
                await job_repo.mark_progress(
                    job,
                    progress=min(95, 2 + int(93 * pages_done / total)),
                    message=f"Extracted {pages_done}/{total} pages…",
                )
    finally:
        parser.close()

    if batch:
        await page_repo.insert_pages(batch)
        await session.commit()
        outcome.pages_persisted += len(batch)
        batch.clear()

    # NOTE: infrastructure faults during persistence propagate unwrapped so
    # the worker applies the transient retry policy (RETRYING + resume).

    outcome.page_count = total or pages_done

    # ── Backfill page_count on the version row (roadmap Phase 5 step 12) ───
    await ver_repo.set_page_count(version.id, outcome.page_count)

    await job_repo.mark_progress(
        job,
        progress=97,
        message=(
            f"Extraction complete: {outcome.page_count} page(s), "
            f"{outcome.ocr_pages} OCR, {outcome.ocr_failed_pages} OCR failed"
        ),
    )
    logger.info(
        "Extraction done for version %s: %d page(s) (%d OCR, %d OCR-failed, "
        "%d resumed/skipped)",
        version.id, outcome.page_count, outcome.ocr_pages,
        outcome.ocr_failed_pages, outcome.pages_skipped,
    )
    return outcome


class _OCRCell:
    """Mutable cell so the OCR provider is constructed lazily (only when some
    page actually routes to OCR) and reused for all subsequent pages."""

    def __init__(self, provider: OCRProvider | None) -> None:
        self.provider = provider


async def _ensure_extracting(
    session: AsyncSession,
    ver_repo: DocumentVersionRepository,
    version: DocumentVersion,
) -> None:
    """PROCESSING → EXTRACTING; mid-pipeline states (resume) stay as-is.

    EXTRACTING → EXTRACTING is not a state-machine transition (strictly
    forward — Backend §47), so a resumed run keeps its current stage.
    """
    current = VersionStatus(version.status)
    if current in (VersionStatus.PROCESSING, VersionStatus.UPLOADED):
        assert_version_transition(current, VersionStatus.EXTRACTING)
        await ver_repo.update_status(version.id, VersionStatus.EXTRACTING.value)
        version.status = VersionStatus.EXTRACTING.value
        await session.commit()
    elif current in (VersionStatus.EXTRACTING, VersionStatus.OCR):
        pass  # resumed mid-pipeline — keep the current stage
    else:  # pragma: no cover — guarded by the worker's claim logic
        assert_version_transition(current, VersionStatus.EXTRACTING)


async def _enter_ocr_stage(
    session: AsyncSession,
    ver_repo: DocumentVersionRepository,
    version: DocumentVersion,
) -> None:
    """EXTRACTING → OCR when the OCR sub-stage begins — exactly once
    (skipped entirely for fully text-native documents, Backend §47)."""
    if version.status != VersionStatus.EXTRACTING.value:
        return  # already OCR (resumed run)
    assert_version_transition(VersionStatus.EXTRACTING, VersionStatus.OCR)
    await ver_repo.update_status(version.id, VersionStatus.OCR.value)
    version.status = VersionStatus.OCR.value
    await session.commit()


async def _recognize_page(
    page: ParsedPage,
    *,
    version: DocumentVersion,
    provider: OCRProvider,
    settings,
    base_metadata: dict,
) -> tuple[str, dict, bool]:
    """OCR one page. Returns (text, metadata, ocr_failed).

    A final OCR failure produces the explicit per-page marker (empty text +
    metadata flag) — the document PROCEEDS, never failing the whole version
    (Backend §19).
    """
    image = await asyncio.to_thread(page.rasterize)  # 300 DPI PNG, off-loop
    context = PageContext(
        page_number=page.page_number,
        document_version_id=version.id,
    )
    try:
        result = await recognize_with_retry(
            provider,
            image,
            context,
            max_attempts=settings.ocr_max_attempts_per_page,
            backoff_seconds=settings.ocr_retry_backoff_seconds,
            timeout_seconds=settings.ocr_timeout_seconds,
        )
    except OCRProviderUnavailable as exc:
        logger.warning(
            "OCR unavailable for version %s page %d: %s",
            version.id, page.page_number, exc,
        )
        return "", {
            "ocr_failed": True,
            "ocr_error": str(exc)[:300],
            **base_metadata,
        }, True
    except OCRPageError as exc:
        logger.warning(
            "OCR failed permanently for version %s page %d after %d "
            "attempt(s): %s",
            version.id, page.page_number, exc.attempts, exc.message,
        )
        return "", {
            "ocr_failed": True,
            "ocr_error": exc.message[:300],
            "ocr_attempts": exc.attempts,
            **base_metadata,
        }, True

    return result.text, {
        "ocr": {
            "provider": result.provider,
            "confidence": result.confidence,
            # Size-bounded: pathological OCR output must not produce
            # unbounded JSONB arrays (Phase 5 security note)
            "lines": [
                {
                    "text": line.text[:200],
                    "bbox": [round(v, 4) for v in line.bbox],
                    "confidence": line.confidence,
                }
                for line in result.lines[: settings.ocr_max_metadata_lines]
            ],
        },
        **base_metadata,
    }, False
