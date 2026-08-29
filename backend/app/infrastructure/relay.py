"""
Redis pub/sub event relay (Phase 11 — multi-instance SSE fan-out, Backend §37).

When the API runs as multiple FastAPI instances behind a load balancer, the
process producing an event (a worker transitioning a processing job, or —
future — a chat generation running on another instance) is frequently NOT
the process holding the client's SSE connection.  A lightweight Redis
pub/sub channel per entity relays events to whichever instance holds the
connection:

    worker ──publish──▶ relay:job:{version_id} ──▶ API instance (SSE for doc)
    API    ──publish──▶ relay:chat:{conversation_id} (future-proofing)

Design:
  - Channels carry WAKE events, not truth: on receipt the SSE endpoint
    re-reads the authoritative state from PostgreSQL and emits only on
    change.  A missed pub/sub message costs at most one poll interval
    (the endpoint also polls on a slow timer) — Redis is disposable
    infrastructure here, exactly as everywhere else in this codebase.
  - Publishing is fire-and-forget: a Redis outage degrades stream latency
    to the poll interval and NEVER breaks processing or chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from app.infrastructure.redis import get_redis

logger = logging.getLogger(__name__)

JOB_CHANNEL_PREFIX = "relay:job:"
CHAT_CHANNEL_PREFIX = "relay:chat:"


def job_channel(document_version_id: str) -> str:
    """Pub/sub channel for one document version's processing-job events."""
    return f"{JOB_CHANNEL_PREFIX}{document_version_id}"


def chat_channel(conversation_id: str) -> str:
    """Pub/sub channel for one conversation's chat events (future-proofing)."""
    return f"{CHAT_CHANNEL_PREFIX}{conversation_id}"


async def publish(channel: str, payload: dict) -> bool:
    """Publish one JSON event; False (logged) when Redis is unavailable."""
    try:
        client = get_redis()
        await client.publish(channel, json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:
        logger.debug("Relay publish failed on %s: %s", channel, exc)
        return False


async def publish_job_event(document_version_id: str) -> bool:
    """Wake subscribers of a version's processing stream (payload minimal)."""
    return await publish(
        job_channel(document_version_id),
        {"type": "job_update", "document_version_id": document_version_id},
    )


async def subscribe_events(
    channels: list[str],
    *,
    poll_interval: float | None = None,
) -> AsyncIterator[dict]:
    """Yield relay events for the subscribed channels until the consumer breaks.

    A ``poll_interval`` additionally yields a synthetic ``{"type": "poll"}``
    event on a timer — the consumer's slow-path re-read trigger that keeps
    the stream correct even when pub/sub messages are missed (Redis restart,
    subscribe race, multi-instance topology quirks).

    When Redis is entirely unavailable (not initialized / connection refused)
    the generator degrades to the pure poll loop — streams stay correct via
    the consumer's re-read-on-every-event design, at poll latency.

    Cleanup (unsubscribe + connection release) happens in ``finally`` — both
    on normal consumer exhaustion and on generator cancellation (client
    disconnect).
    """
    if not channels:
        return
    try:
        client = get_redis()
    except Exception as exc:  # Redis unavailable — pure-poll degradation
        logger.debug("Relay unavailable (%s) — falling back to polling", exc)
        async for event in _poll_only(poll_interval):
            yield event
        return
    try:
        pubsub = client.pubsub()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Relay pubsub unavailable (%s) — falling back to polling", exc)
        async for event in _poll_only(poll_interval):
            yield event
        return
    try:
        await pubsub.subscribe(*channels)
        if poll_interval is not None and poll_interval > 0:
            async for event in _listen_with_polling(pubsub, poll_interval):
                yield event
        else:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                event = _parse(message.get("data"))
                if event is not None:
                    yield event
    finally:
        try:
            await pubsub.unsubscribe(*channels)
        except Exception:  # pragma: no cover — cleanup best-effort
            pass
        try:
            await pubsub.aclose()
        except AttributeError:  # older redis-py
            await pubsub.close()
        except Exception:  # pragma: no cover — cleanup best-effort
            pass


async def _poll_only(poll_interval: float | None) -> AsyncIterator[dict]:
    """Synthetic poll events when pub/sub is unavailable (degraded mode)."""
    interval = poll_interval if poll_interval and poll_interval > 0 else 2.0
    while True:
        await asyncio.sleep(interval)
        yield {"type": "poll"}


def _parse(raw: object) -> dict | None:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parsed = json.loads(raw)  # type: ignore[arg-type]
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


async def _listen_with_polling(
    pubsub,  # type: ignore[no-untyped-def]
    poll_interval: float,
) -> AsyncIterator[dict]:
    """Merge pub/sub messages with a periodic synthetic poll event.

    Uses ``pubsub.listen()`` racing an ``asyncio.sleep`` timer (via
    ``asyncio.wait``) rather than ``get_message(timeout=…)`` so behaviour
    never depends on redis-py's blocking-timeout semantics.
    """
    listener = pubsub.listen()
    listen_task: asyncio.Task = asyncio.ensure_future(listener.__anext__())
    try:
        while True:
            poll_task: asyncio.Task = asyncio.ensure_future(
                asyncio.sleep(poll_interval)
            )
            done, _pending = await asyncio.wait(
                {listen_task, poll_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if listen_task in done:
                try:
                    message = listen_task.result()
                except StopAsyncIteration:
                    poll_task.cancel()
                    return
                if message.get("type") == "message":
                    event = _parse(message.get("data"))
                    if event is not None:
                        yield event
                listen_task = asyncio.ensure_future(listener.__anext__())
            if poll_task in done:
                yield {"type": "poll"}
            else:
                poll_task.cancel()
    finally:
        listen_task.cancel()
        try:
            await listener.aclose()
        except Exception:  # pragma: no cover — cleanup best-effort
            pass
