"""
Arq queue infrastructure — Redis-backed job pointers.

Design (Backend §23/§24 — the durable-in-PostgreSQL / pointer-in-Redis split):

  - `processing_jobs` rows (PostgreSQL) are the authoritative job state.
  - Redis entries are lightweight pointers: a job function name plus the
    processing_job UUID. Nothing about job state lives in Redis.
  - Every enqueue uses an explicit deterministic Arq job id (`pj:<uuid>`) so:
      * enqueueing the same job twice is deduplicated by Arq, and
      * the reconciliation sweep can blindly re-enqueue candidates — live
        entries are skipped automatically.

Queues:
  - `default` — ingestion stages (EXTRACTION/OCR/CHUNKING/EMBEDDING/INDEXING)
  - `low`     — maintenance/housekeeping (sweep, purge) so bursts of uploads
                never starve time-insensitive jobs (Backend §24)

This module is infrastructure: nothing above it imports `arq` directly.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ── Queue names (full Redis keys — Arq convention) ────────────────────────────

QUEUE_DEFAULT = "arq:queue"
QUEUE_LOW = "arq:low"

# Redis dead-letter list key (operations visibility only — PostgreSQL is truth)
DEAD_LETTER_KEY = "aidoc:deadletter"

# Arq job-function names registered by the worker
JOB_FUNCTION_RUN = "run_processing_job"
JOB_FUNCTION_SWEEP = "reconciliation_sweep"

_arq_pool: ArqRedis | None = None


async def init_queue_pool() -> None:
    """Create the Arq Redis pool used for enqueueing from the API process.

    The worker process uses its own pool created by arq's WorkerSettings.
    Called from the FastAPI lifespan startup handler.
    """
    global _arq_pool
    if _arq_pool is not None:
        return
    settings = get_settings()
    _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    logger.info("Arq enqueue pool initialized.")


async def close_queue_pool() -> None:
    """Close the Arq enqueue pool (FastAPI lifespan shutdown)."""
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.close()
        logger.info("Arq enqueue pool closed.")
        _arq_pool = None


def get_queue_pool() -> ArqRedis:
    """Return the Arq pool. Raises RuntimeError if not initialized."""
    if _arq_pool is None:
        raise RuntimeError("Queue pool not initialized — call init_queue_pool() first.")
    return _arq_pool


def pointer_job_id(processing_job_id: str) -> str:
    """Deterministic Arq job id for a processing_job pointer (`pj:<uuid>`)."""
    return f"pj:{processing_job_id}"


async def enqueue_processing_job(
    job_id: str,
    *,
    queue_name: str = QUEUE_DEFAULT,
    delay_seconds: int | None = None,
) -> bool:
    """Enqueue a lightweight pointer telling a worker to run `job_id`.

    MUST be called only AFTER the transaction that created the PENDING
    processing_jobs row has committed (Backend §50 — never inside it).
    If the enqueue fails here the sweep recovers the gap.

    Args:
        job_id: The processing_jobs row UUID (NOT the Arq job id).
        queue_name: `default` (ingestion) or `low` (maintenance).
        delay_seconds: If set, defer execution (retry backoff scheduling).

    Returns:
        True if a new pointer was enqueued; False if Arq deduplicated it
        (a live pointer for this job already exists).
    """
    pool = get_queue_pool()
    kwargs: dict[str, Any] = {
        "_job_id": pointer_job_id(job_id),
        "_queue_name": queue_name,
    }
    if delay_seconds is not None and delay_seconds > 0:
        kwargs["_defer_by"] = timedelta(seconds=delay_seconds)

    arq_job = await pool.enqueue_job(JOB_FUNCTION_RUN, job_id, **kwargs)
    enqueued = arq_job is not None
    if not enqueued:
        logger.debug(
            "Enqueue deduplicated — live pointer already exists for job %s", job_id
        )
    return enqueued


async def enqueue_sweep(
    *,
    queue_name: str = QUEUE_LOW,
    delay_seconds: int | None = None,
) -> bool:
    """Enqueue a reconciliation-sweep pointer (used by cron / tests)."""
    pool = get_queue_pool()
    kwargs: dict[str, Any] = {
        "_job_id": f"sweep:{queue_name}",
        "_queue_name": queue_name,
    }
    if delay_seconds is not None and delay_seconds > 0:
        kwargs["_defer_by"] = timedelta(seconds=delay_seconds)
    arq_job = await pool.enqueue_job(JOB_FUNCTION_SWEEP, **kwargs)
    return arq_job is not None


async def push_dead_letter(record: dict[str, Any]) -> None:
    """Push a compact failure record onto the Redis dead-letter list.

    Best-effort operational visibility for on-call (Backend §23) — PostgreSQL
    remains the authoritative record, so failures here are logged, never
    raised. Records carry job METADATA only, never document content.
    """
    try:
        pool = get_queue_pool()
        await pool.lpush(DEAD_LETTER_KEY, json.dumps(record, default=str))
        # Bound the list so it cannot grow without limit
        await pool.ltrim(DEAD_LETTER_KEY, 0, 999)
    except Exception as exc:  # noqa: BLE001 — dead-letter is best-effort
        logger.warning("Failed to push dead-letter record: %s", exc)


async def read_dead_letter(count: int = 50) -> list[dict[str, Any]]:
    """Read the most recent dead-letter records (ops tooling / diagnostics)."""
    pool = get_queue_pool()
    raw = await pool.lrange(DEAD_LETTER_KEY, 0, count - 1)
    return [json.loads(item) for item in raw]
