"""
Chat generation stop flags (Phase 11 — explicit cancellation, Backend §37).

`POST /chat/messages/{id}/stop` sets a short-lived, message-scoped Redis
key; the streaming generator checks it BETWEEN token chunks and freezes the
partial answer when it fires.  Key properties:

  - Message-scoped: no cross-conversation (let alone cross-user) interference.
  - Short TTL: a stop request for a request that already finished (or never
    started) expires on its own — no manual cleanup, no unbounded growth.
  - Fail-open: a Redis outage NEVER breaks generation — the flag check
    degrades to "not stopped" and the answer streams to completion.
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.infrastructure.redis import get_redis

logger = logging.getLogger(__name__)

_STOP_KEY_PREFIX = "chat:stop:"


def _key(message_id: str) -> str:
    return f"{_STOP_KEY_PREFIX}{message_id}"


async def request_stop(message_id: str) -> bool:
    """Set the stop flag for one message (idempotent).

    Returns True when the flag is set, False when Redis is unavailable (the
    stop endpoint then reports that cancellation could not be guaranteed).
    """
    ttl = get_settings().chat_stop_flag_ttl_seconds
    try:
        client = get_redis()
        await client.set(_key(message_id), "1", ex=ttl)
        return True
    except Exception as exc:
        logger.warning("Stop flag write failed for message %s: %s", message_id, exc)
        return False


async def is_stop_requested(message_id: str) -> bool:
    """True when a stop was requested for this message (fail-open on errors)."""
    try:
        client = get_redis()
        return bool(await client.exists(_key(message_id)))
    except Exception as exc:
        logger.debug("Stop flag read failed for message %s: %s", message_id, exc)
        return False


async def clear_stop(message_id: str) -> None:
    """Best-effort flag cleanup (the TTL is the real safety net)."""
    try:
        client = get_redis()
        await client.delete(_key(message_id))
    except Exception:  # pragma: no cover — best-effort by contract
        pass
