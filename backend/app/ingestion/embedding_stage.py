"""
Embedding stage orchestrator — Phase 7 (Backend §22; roadmap Phase 7 step 3).

Responsibilities:
  1. Load unembedded chunks for a document version in reading order.
  2. Call the EmbeddingProvider in batches of ``embedding_batch_size``
     (default 100).
  3. Write each batch's vectors immediately to ``document_chunks.embedding``
     and commit — incremental checkpointing so a worker crash never re-bills
     already-embedded batches (Backend §49).
  4. Record token-count attribution per batch (organisation, version) for
     the Phase 19 cost-tracking system — stored as worker-level log data
     now; the formal reporting store arrives with Phase 19.
  5. Transition the version status CHUNKING → EMBEDDING at stage start.
  6. Report progress via processing_jobs.progress (0–100 %).

Idempotency: already-embedded chunks are skipped (``embedding IS NOT NULL``);
re-running the stage only embeds the un-covered tail.

Error taxonomy:
  EmbeddingError       — deterministic (dimension mismatch, auth failure) →
                          worker converts to DeterministicJobError → FAILED.
  EmbeddingProviderError — transient (network, 5xx, rate-limit) →
                          propagates as a generic exception → Phase 4 retry.

See:
  Backend-Architecture-Documentation.md §22 (Embedding Pipeline)
  Database-Architecture-Design-Documentation.md §17 (pgvector / model pinning)
  roadmap Phase 7, steps 3–5
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.state_machines import assert_version_transition
from app.infrastructure.embeddings import (
    EmbeddingDimensionError,
    EmbeddingProvider,
    EmbeddingProviderError,
    get_embedding_provider,
)
from app.models.document import Document, DocumentVersion
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.document_repository import DocumentVersionRepository
from app.repositories.processing_job_repository import (
    ProcessingJob,
    ProcessingJobRepository,
)

logger = logging.getLogger(__name__)


# ── Error types ────────────────────────────────────────────────────────────────

class EmbeddingError(Exception):
    """Deterministic embedding failure — retrying will not help.

    Examples: dimension mismatch (model changed without migration), API-key
    auth error.  The worker converts this to DeterministicJobError → FAILED.
    """

    code: str = "EMBEDDING_FAILED"

    def __init__(self, message: str, *, code: str = "EMBEDDING_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ── Stage entry point ──────────────────────────────────────────────────────────

async def run_embedding(
    session: AsyncSession,
    *,
    version: DocumentVersion,
    document: Document,
    job: ProcessingJob,
    job_repo: ProcessingJobRepository,
    ver_repo: DocumentVersionRepository,
    provider: EmbeddingProvider | None = None,
) -> None:
    """Embed all chunks for a document version.

    Args:
        session:     The active SQLAlchemy async session.
        version:     The DocumentVersion being embedded.
        document:    The parent Document (for org/logging context).
        job:         The current EMBEDDING ProcessingJob row.
        job_repo:    ProcessingJobRepository for progress updates.
        ver_repo:    DocumentVersionRepository for status transitions.
        provider:    EmbeddingProvider to use (defaults to the process-global
                     singleton — injectable for tests).

    Raises:
        EmbeddingError:         On deterministic failures (dim mismatch, auth).
        EmbeddingProviderError: On transient failures (retried by worker).
    """
    settings = get_settings()
    embedding_provider = provider or get_embedding_provider()
    chunk_repo = DocumentChunkRepository(session)

    # ── Status transition: current stage → EMBEDDING ───────────────────────
    from app.domain.documents import VersionStatus
    current_status = VersionStatus(version.status)
    # Accept CHUNKING (normal) or EMBEDDING (resume after crash)
    if current_status is not VersionStatus.EMBEDDING:
        assert_version_transition(current_status, VersionStatus.EMBEDDING)
        await ver_repo.update_status(version.id, VersionStatus.EMBEDDING.value)
        await session.commit()
    else:
        logger.info(
            "Embedding stage resuming (already EMBEDDING): version %s", version.id
        )

    await job_repo.mark_progress(job, progress=2, message="Counting chunks to embed…")

    # ── Count total chunks (for progress reporting) ────────────────────────
    total_chunks = await chunk_repo.count_for_version(version.id)
    if total_chunks == 0:
        logger.warning(
            "Version %s has no chunks — skipping embedding stage", version.id
        )
        return

    already_embedded = await chunk_repo.count_embedded_for_version(version.id)
    remaining = total_chunks - already_embedded

    if remaining == 0:
        logger.info(
            "Version %s: all %d chunks already embedded — embedding stage is a no-op",
            version.id, total_chunks,
        )
        return

    logger.info(
        "Embedding version %s: %d total chunks, %d already done, %d remaining",
        version.id, total_chunks, already_embedded, remaining,
    )

    # ── Batch embedding loop ───────────────────────────────────────────────
    batch_size = settings.embedding_batch_size
    model_name = embedding_provider.model_name
    total_tokens_estimated = 0
    chunks_done = already_embedded

    # Pagination offset into the unembedded set (always 0 within the loop
    # because each batch is committed and removed from the unembedded set
    # before the next iteration loads).
    while True:
        # Load the next batch of unembedded chunks (offset=0 because
        # committed rows are no longer returned by the query)
        batch = await chunk_repo.list_unembedded_for_version(
            version.id, limit=batch_size
        )
        if not batch:
            break  # All chunks embedded

        texts = [chunk.content for chunk in batch]
        chunk_ids = [chunk.id for chunk in batch]
        token_counts = [chunk.token_count for chunk in batch]

        batch_num = math.ceil((chunks_done - already_embedded + len(batch)) / batch_size)
        total_batches = math.ceil(remaining / batch_size)

        logger.info(
            "Embedding batch %d/%d: %d chunks for version %s",
            batch_num, total_batches, len(texts), version.id,
        )

        # ── Call the provider ──────────────────────────────────────────────
        try:
            vectors = await embedding_provider.embed(texts)
        except EmbeddingDimensionError as exc:
            raise EmbeddingError(str(exc), code="EMBEDDING_DIMENSION_MISMATCH") from exc
        except EmbeddingProviderError:
            # Transient — propagate to the worker's retry policy
            raise

        # ── Validate response length ───────────────────────────────────────
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"Provider returned {len(vectors)} vectors for {len(texts)} texts "
                f"(version {version.id}, batch starting chunk_id={chunk_ids[0]})",
                code="EMBEDDING_LENGTH_MISMATCH",
            )

        # ── Write batch to the database + commit (checkpoint) ─────────────
        updates = [
            {
                "chunk_id": chunk_id,
                "embedding": vector,
                "embedding_model": model_name,
            }
            for chunk_id, vector in zip(chunk_ids, vectors)
        ]

        written = await chunk_repo.update_embeddings_batch_raw(updates)
        await session.commit()  # checkpoint — crash here resumes from next batch

        chunks_done += written
        total_tokens_estimated += sum(token_counts)

        # Cost attribution logging (Phase 19 will persist this formally)
        logger.info(
            "Embedded batch: version=%s, chunks_written=%d, "
            "cumulative_chunks=%d/%d, estimated_tokens_in_batch=%d, "
            "cumulative_tokens=%d, model=%s, org=%s",
            version.id,
            written,
            chunks_done,
            total_chunks,
            sum(token_counts),
            total_tokens_estimated,
            model_name,
            document.organization_id,
        )

        # Progress: 2 % reserved for init; 98 % for embedding work
        progress = 2 + int(97 * chunks_done / total_chunks)
        await job_repo.mark_progress(
            job,
            progress=min(progress, 99),
            message=f"Embedding {chunks_done}/{total_chunks} chunks…",
        )
        await session.commit()

    # ── Final validation ───────────────────────────────────────────────────
    final_embedded = await chunk_repo.count_embedded_for_version(version.id)
    if final_embedded < total_chunks:
        raise EmbeddingError(
            f"Embedding stage completed but only {final_embedded}/{total_chunks} "
            f"chunks have embeddings (version {version.id}). "
            f"Partial results — this job should be retried.",
            code="EMBEDDING_INCOMPLETE",
        )

    logger.info(
        "Embedding stage complete: version=%s, total_chunks=%d, "
        "estimated_tokens=%d, model=%s",
        version.id, total_chunks, total_tokens_estimated, model_name,
    )
