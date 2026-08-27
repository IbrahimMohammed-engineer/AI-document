"""
Arq worker entrypoint — Phase 4 stub.

This module exists to satisfy the Docker Compose worker service command
`python -m app.workers.main` during Phase 0/1. The actual job definitions
(extraction, OCR, chunking, embedding, purge) will be added in Phase 4.

In Phase 4, this module will define:
  - WorkerSettings class with the Arq Redis settings
  - `functions` list of job handler coroutines
  - `on_startup` / `on_shutdown` hooks for DB and storage init
"""
from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


async def _idle_loop() -> None:
    """Keep the worker process alive in Phase 0/1 (no jobs yet)."""
    logger.info(
        "Arq worker started (Phase 4 stub — no jobs registered yet). "
        "Will idle until jobs are implemented in Phase 4."
    )
    stop_event = asyncio.Event()

    def _handle_signal(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()
    logger.info("Worker shutting down.")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=logging.INFO)
    asyncio.run(_idle_loop())
