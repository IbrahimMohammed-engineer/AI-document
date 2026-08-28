"""
Chunking stage orchestrator (roadmap Phase 6; Backend §20/§21).

Consumes the structure detector + chunker and produces `document_sections`
and `document_chunks` rows:

    load stored pages → (PDF: font/table signal extraction from the
    original bytes) → heuristic section detection → persist section tree
    → structure-aware chunking (pure rules) → batched UPSERT persistence
    (committed per batch so a crash resumes without duplicating)

Status transitions owned here:
    EXTRACTING|OCR|PROCESSING → CHUNKING   at stage start (idempotent
    on resume — a resumed run keeps CHUNKING)

Idempotency (Backend §49; roadmap Phase 6 step 8): sections are deleted +
re-inserted per run (a re-run overwrites, never duplicates); chunks are
UPSERTed on (document_version_id, chunk_index) with per-batch commit
checkpoints, and rows beyond the new count are removed first, so the
persisted set is exact across re-runs.

Content source of truth: chunks are built from the STORED page text (what
extraction committed — including OCR text), never from a re-parse of the
original file; the original bytes are only consulted for font/table
SIGNALS. Chunk provenance therefore always matches the pages table.

Error taxonomy: ChunkingErrors are DETERMINISTIC (terminal FAILED);
infrastructure faults propagate to the worker's generic retry policy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.documents import VersionStatus, compute_sha256
from app.domain.state_machines import assert_version_transition
from app.infrastructure.storage import (
    ObjectStorageProvider,
    StorageObjectMissingError,
    get_storage_provider,
)
from app.ingestion.chunker import ChunkingPlan, chunk_document
from app.ingestion.structure_detector import (
    PdfFontInfo,
    build_page_lines,
    detect_sections,
    extract_pdf_font_info,
)
from app.ingestion.tokenizer import counter
from app.models.document import Document, DocumentVersion
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.document_page_repository import DocumentPageRepository
from app.repositories.document_repository import DocumentVersionRepository
from app.repositories.document_section_repository import (
    DocumentSectionRepository,
)
from app.repositories.processing_job_repository import (
    ProcessingJob,
    ProcessingJobRepository,
)

logger = logging.getLogger(__name__)

# Metadata size bounds — pathological documents must not produce unbounded
# JSONB (Phase 6 security note, mirroring the Phase 5 OCR-metadata bound).
_MAX_HEADING_ENTRY_CHARS = 200
_MAX_TABLES_PER_CHUNK = 20


class ChunkingError(Exception):
    """Chunking cannot proceed, and retrying will not change that.

    Raised by the stage; the worker converts this into
    DeterministicJobError → terminal FAILED (Backend §48).
    """

    code: str = "CHUNKING_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


@dataclass
class ChunkingOutcome:
    """Summary of one chunking run (observability + tests)."""

    page_count: int = 0
    section_count: int = 0
    structure_detected: bool = False
    chunk_count: int = 0
    chunks_persisted: int = 0
    table_pages: int = 0
    tokenizer_engine: str = ""


async def run_chunking(
    session: AsyncSession,
    *,
    version: DocumentVersion,
    document: Document,
    job: ProcessingJob,
    job_repo: ProcessingJobRepository,
    ver_repo: DocumentVersionRepository,
    storage: ObjectStorageProvider | None = None,
) -> ChunkingOutcome:
    """Run the full CHUNKING stage for one document version.

    The handler wrapper (workers/jobs.py) translates ChunkingError into
    DeterministicJobError; transient faults propagate for the Phase 4
    retry policy. Chunk batches are committed HERE so crashes resume.
    """
    settings = get_settings()
    page_repo = DocumentPageRepository(session)
    section_repo = DocumentSectionRepository(session)
    chunk_repo = DocumentChunkRepository(session)
    storage = storage or get_storage_provider()

    # ── Version status: → CHUNKING (idempotent on resume) ─────────────────
    await _enter_chunking(session, ver_repo, version)

    await job_repo.mark_progress(job, progress=3, message="Loading extracted pages…")

    # ── The stored pages are the content source of truth ──────────────────
    pages = await page_repo.list_for_version(version.id)
    if not pages:
        # Extraction claimed completion without pages — a pipeline bug or
        # external data loss; retrying cannot fix it (deterministic).
        raise ChunkingError(
            "No extracted pages found for this version — cannot chunk. "
            "Retry processing from extraction.",
            code="CHUNK_NO_PAGES",
        )
    outcome = ChunkingOutcome(page_count=len(pages))
    pages_by_number = {p.page_number: p for p in pages}

    # ── Font/table signals from the original bytes (PDF, best-effort) ─────
    font_info: PdfFontInfo | None = None
    if version.mime_type == "application/pdf":
        try:
            data = await storage.download(version.storage_key)
        except StorageObjectMissingError as exc:
            raise ChunkingError(
                "Original file object is missing from storage — cannot "
                "extract structure signals. Re-upload the document.",
                code="SOURCE_OBJECT_MISSING",
            ) from exc
        try:
            font_info = extract_pdf_font_info(data)
        except Exception as exc:  # noqa: BLE001 — signals are optional
            logger.warning(
                "Font-signal extraction failed for version %s — detection "
                "continues on numbering patterns alone: %s",
                version.id,
                exc,
            )
            font_info = None

    await job_repo.mark_progress(
        job, progress=8, message="Detecting document structure…"
    )

    # ── Structure detection (heuristics — Backend §20) ────────────────────
    pages_lines = [
        build_page_lines(page.page_number, page.text, font_info) for page in pages
    ]
    sections = detect_sections(pages_lines, font_info)
    outcome.section_count = len(sections)
    outcome.structure_detected = bool(sections)
    outcome.tokenizer_engine = counter.engine_name()

    # ── Idempotent reset: re-runs overwrite, never duplicate ──────────────
    await chunk_repo.delete_for_version(version.id)
    await section_repo.delete_for_version(version.id)
    await session.flush()

    # ── Persist the section tree (sort_order → persisted ID map) ──────────
    section_ids: dict[int, str] = {}
    if sections:
        rows = []
        for section in sections:
            rows.append(
                {
                    "document_version_id": version.id,
                    "parent_section_id": None,  # resolved below, post-insert
                    "title": section.title[: _MAX_HEADING_ENTRY_CHARS],
                    "section_number": section.section_number,
                    "start_page": section.start_page,
                    "end_page": section.end_page,
                    "sort_order": section.sort_order,
                }
            )
        instances = await section_repo.insert_sections(rows)
        for instance, section in zip(instances, sections):
            section_ids[section.sort_order] = instance.id
        # Second pass: resolve self-referencing parents
        for instance, section in zip(instances, sections):
            parent_sort = section.parent_sort_order
            if parent_sort is not None:
                instance.parent_section_id = section_ids.get(parent_sort)
        await session.flush()

    await job_repo.mark_progress(
        job,
        progress=20,
        message=(
            f"Structure detected: {len(sections)} section(s)…"
            if sections
            else "No structure detected — page-level chunking…"
        ),
    )

    # ── Structure-aware chunking (pure rules — Backend §21) ───────────────
    plan: ChunkingPlan = chunk_document(
        pages_lines,
        sections,
        count=counter.count_tokens,
        target_min_tokens=settings.chunk_target_min_tokens,
        target_max_tokens=settings.chunk_target_max_tokens,
        hard_max_tokens=settings.chunk_hard_max_tokens,
        overlap_tokens=int(
            settings.chunk_overlap_ratio
            * (settings.chunk_target_min_tokens + settings.chunk_target_max_tokens)
            / 2
        ),
        table_pages=sorted(font_info.tables) if font_info else [],
    )
    outcome.chunk_count = len(plan.chunks)
    outcome.table_pages = len(plan.table_pages)

    # ── Batched UPSERT persistence (commit checkpoints — Backend §49) ─────
    batch_size = max(1, settings.chunking_batch_size)
    persisted = 0
    for start in range(0, len(plan.chunks), batch_size):
        batch = plan.chunks[start : start + batch_size]
        rows = [
            _chunk_row(chunk, version, document, pages_by_number, section_ids,
                       settings)
            for chunk in batch
        ]
        await chunk_repo.upsert_chunks(rows)
        await session.commit()  # durability checkpoint — resume anchor
        persisted += len(batch)
        outcome.chunks_persisted = persisted
        await job_repo.mark_progress(
            job,
            progress=min(95, 20 + int(75 * persisted / max(1, len(plan.chunks)))),
            message=f"Chunked {persisted}/{len(plan.chunks)} chunks…",
        )

    await job_repo.mark_progress(
        job,
        progress=97,
        message=(
            f"Chunking complete: {len(sections)} section(s), "
            f"{len(plan.chunks)} chunk(s)"
        ),
    )
    logger.info(
        "Chunking done for version %s: %d section(s), %d chunk(s) "
        "(%d table page(s), tokenizer=%s)",
        version.id,
        len(sections),
        len(plan.chunks),
        len(plan.table_pages),
        outcome.tokenizer_engine,
    )
    return outcome


def _chunk_row(
    chunk,
    version: DocumentVersion,
    document: Document,
    pages_by_number: dict,
    section_ids: dict[int, str],
    settings,
) -> dict:
    """Assemble one document_chunks INSERT row (provenance-complete)."""
    start_page = pages_by_number.get(chunk.start_page)
    if start_page is None:
        raise ChunkingError(
            f"Chunk references page {chunk.start_page} which has no "
            "persisted row — inconsistent page store.",
            code="CHUNK_PAGE_MISSING",
        )
    end_page = pages_by_number.get(chunk.end_page) if chunk.end_page else None

    heading_path = [
        title[:_MAX_HEADING_ENTRY_CHARS]
        for title in chunk.heading_path[: settings.chunk_max_heading_path_depth]
    ]
    metadata: dict = {
        "heading_path": heading_path,
        "section_number": chunk.section_number,
        "contains_table": chunk.contains_table,
        "contains_list": chunk.contains_list,
        "page_span": [chunk.start_page, chunk.end_page],
    }
    if chunk.forced_split:
        metadata["forced_split"] = True

    return {
        "organization_id": document.organization_id,
        "document_version_id": version.id,
        "page_id": start_page.id,
        "end_page_id": end_page.id if end_page is not None and chunk.end_page != chunk.start_page else None,
        "section_id": section_ids.get(chunk.section_sort_order)
        if chunk.section_sort_order is not None
        else None,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "content_hash": compute_sha256(chunk.content.encode("utf-8")),
        "token_count": chunk.token_count,
        "metadata": metadata,
    }


async def _enter_chunking(
    session: AsyncSession,
    ver_repo: DocumentVersionRepository,
    version: DocumentVersion,
) -> None:
    """→ CHUNKING at stage start; a resumed run keeps its current stage.

    CHUNKING is strictly forward from every pre-chunking pipeline state
    (Backend §47), so EXTRACTING/OCR/PROCESSING all transition here.
    """
    current = VersionStatus(version.status)
    if current is VersionStatus.CHUNKING:
        return  # resumed mid-stage
    if current is VersionStatus.READY:  # pragma: no cover — worker guards
        return
    assert_version_transition(current, VersionStatus.CHUNKING)
    await ver_repo.update_status(version.id, VersionStatus.CHUNKING.value)
    version.status = VersionStatus.CHUNKING.value
    await session.commit()
