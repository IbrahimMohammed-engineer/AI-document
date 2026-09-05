"""
Arq worker entrypoint — Phase 4.

Separately deployable from the FastAPI API process (same codebase, Backend §8):
    python -m app.workers.main          (or: arq app.workers.main.WorkerSettings)

Queue selection via WORKER_QUEUE_NAME:
    default — ingestion stages (EXTRACTION/OCR/CHUNKING/EMBEDDING/INDEXING)
    low     — maintenance/housekeeping (reconciliation sweep, future purge)

Startup performs the initial reconciliation sweep (Backend §23): any PENDING /
RETRYING / stuck-PROCESSING job in PostgreSQL without a live Redis pointer is
re-enqueued — making "Redis flushed" a non-event.
"""
from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.workers.jobs import (
    reconciliation_sweep,
    run_processing_job,
    run_retention_purge,
    trigger_conflict_scans,
)

settings = get_settings()
setup_logging(log_level=settings.log_level)
logger = logging.getLogger(__name__)


# ── Lifecycle hooks ───────────────────────────────────────────────────────────

async def on_startup(ctx: dict) -> None:
    """Initialize the same infrastructure the API uses, then sweep."""
    logger.info(
        "Worker starting — queue=%r max_jobs=%d",
        settings.worker_queue_name,
        settings.worker_max_jobs,
    )

    from app.infrastructure.database import init_db
    from app.infrastructure.queue import init_queue_pool
    from app.infrastructure.storage import (
        create_storage_provider_from_settings,
        set_storage_provider,
    )

    await init_db()
    await init_queue_pool()

    # Storage provider (job handlers read/verify objects)
    try:
        set_storage_provider(create_storage_provider_from_settings())
    except Exception as exc:
        logger.error("Failed to initialize storage provider: %s", exc)
        # Handlers will fail with a retryable error until storage returns

    # Initial reconciliation sweep — recover anything missed while down
    try:
        recovered = await reconciliation_sweep(ctx)
        logger.info("Startup reconciliation sweep re-enqueued %d job(s).", recovered)
    except Exception:
        logger.exception("Startup reconciliation sweep failed — cron will retry.")


async def on_shutdown(ctx: dict) -> None:
    from app.infrastructure.database import close_db
    from app.infrastructure.queue import close_queue_pool

    await close_queue_pool()
    await close_db()
    logger.info("Worker shut down.")


# ── Worker settings ───────────────────────────────────────────────────────────

class WorkerSettings:
    """Arq worker configuration (consumed by `arq` / run_worker)."""

    functions = [
        run_processing_job,
        reconciliation_sweep,
        trigger_conflict_scans,
        run_retention_purge,
    ]

    # Periodic reconciliation sweep — cron fields are wall-clock sets, so the
    # configured interval maps onto the seconds within each minute (an interval
    # >= 60s simply runs once per minute at second 0).
    _sweep_interval = max(1, settings.reconciliation_sweep_interval_seconds)
    _sweep_second: int | set[int] = (
        set(range(0, 60, _sweep_interval)) if _sweep_interval < 60 else 0
    )
    cron_jobs = [
        cron(
            reconciliation_sweep,
            second=_sweep_second,
            unique=True,
            run_at_startup=True,
            max_tries=1,
        ),
        # Phase 13: nightly org-wide conflict scan (V1 — one fixed cadence;
        # post-batch-READY triggering and per-org windows are deferred
        # enhancements, plan §15).
        cron(
            trigger_conflict_scans,
            hour=settings.conflict_scan_hour,
            minute=0,
            second=0,
            unique=True,
            run_at_startup=False,
            max_tries=1,
        ),
        # Phase 16: nightly retention hard-purge — soft-deleted documents
        # past the retention window lose their DB rows and storage objects
        # (plan §7). Default 03:00, one hour after the conflict scan.
        cron(
            run_retention_purge,
            hour=settings.retention_purge_hour,
            minute=0,
            second=0,
            unique=True,
            run_at_startup=False,
            max_tries=1,
        ),
    ]

    on_startup = on_startup
    on_shutdown = on_shutdown

    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # This process consumes exactly one queue (deploy one process per queue)
    queue_name = settings.worker_queue_name

    max_jobs = settings.worker_max_jobs
    # Job retries are owned by our policy (RETRYING state + backoff); Arq
    # retries only cover infrastructure-level failures of the pointer itself.
    max_tries = 10
    job_timeout = 1800        # 30 min hard cap per job execution
    keep_result = 300         # results are pointers' metadata — short retention
    health_check_interval = 15


if __name__ == "__main__":
    from arq.worker import run_worker

    run_worker(WorkerSettings)  # type: ignore[arg-type]  # pragma: no cover
