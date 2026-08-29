"""
Document processing SSE streams (Phase 11 — FE §11.2, Backend §37).

Endpoints (declared on a SEPARATE router registered BEFORE the documents
router, so the multiplexed ``/documents/stream`` never collides with
``/documents/{document_id}``):

  GET /documents/{id}/stream            — one document's processing events
  GET /documents/stream?ids=a,b,c       — multiplexed (header indicator)

Event protocol (FE §11.2):

    event: status  data: {…DocumentStatusResponse…}      (initial state first)
    event: status  data: {…changed fields…}              (on every change)
    (connection closes once every tracked document is READY/FAILED)

Truth comes from PostgreSQL; the Redis relay (``relay:job:{version_id}``
channels — fed by the worker's job-state writes) provides the low-latency
wake-up, and a slow poll re-read (default 2 s) guarantees correctness even
when pub/sub messages are missed (Redis restart, multi-instance races).
Heartbeat comments keep proxies from closing idle connections.  A client
reconnect simply re-opens the stream (the initial event re-syncs state) —
the stream is an optimization, REST remains the source of truth (FE §11.3).

See:
  Frontend-Design-Documentation.md §11 (Real-Time Processing UX)
  Backend-Architecture-Documentation.md §37 (Streaming — multi-instance fan-out)
  roadmap Phase 11 steps 7–8
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import DbSession, require_permission
from app.core.config import get_settings
from app.domain.permissions import PermissionKey
from app.infrastructure.relay import subscribe_events
from app.models.user import User
from app.schemas.document import DocumentStatusResponse
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _is_terminal(payload: dict) -> bool:
    return payload.get("status") in ("READY", "FAILED")


async def _status_of(db, user: User, document_id: str) -> DocumentStatusResponse:
    return await JobService.get_document_processing_status(
        document_id=document_id,
        organization_id=user.organization_id,
        db=db,
    )


def _payload(status: DocumentStatusResponse, *, include_id: bool) -> dict:
    data = status.model_dump(mode="json")
    if not include_id:
        data.pop("document_id", None)
    return data


async def _stream_states(
    *,
    db,
    user: User,
    request: Request,
    document_ids: list[str],
    include_document_id: bool,
) -> AsyncIterator[str]:
    """Shared multi-document streaming loop (relay wake + poll + heartbeat)."""
    settings = get_settings()
    poll_interval = settings.stream_poll_seconds
    heartbeat_interval = settings.sse_heartbeat_seconds

    # ── Initial state (REST-fallback parity — FE §11.3) ───────────────────
    last_sent: dict[str, dict] = {}
    version_ids: list[str] = []
    for document_id in document_ids:
        status = await _status_of(db, user, document_id)
        payload = _payload(status, include_id=include_document_id)
        last_sent[document_id] = payload
        if status.version_id and status.version_id not in version_ids:
            version_ids.append(status.version_id)
        yield _sse("status", payload)

    if all(_is_terminal(payload) for payload in last_sent.values()):
        return  # nothing in flight — the stream is already done

    async def _refresh() -> list[str]:
        """Re-read non-terminal documents; return SSE frames for changes."""
        frames: list[str] = []
        for document_id, current in list(last_sent.items()):
            if _is_terminal(current):
                continue  # terminal rows never re-emit
            status = await _status_of(db, user, document_id)
            payload = _payload(status, include_id=include_document_id)
            if payload != current:
                last_sent[document_id] = payload
                if status.version_id and status.version_id not in version_ids:
                    # Version changed mid-stream (new upload) — track its channel
                    version_ids.append(status.version_id)
                frames.append(_sse("status", payload))
        return frames

    last_output = time.monotonic()
    try:
        async for _relay_event in subscribe_events(
            [f"relay:job:{vid}" for vid in version_ids],
            poll_interval=poll_interval,
        ):
            # Disconnect check on every wake (Backend §37 — cheap + responsive)
            try:
                if await request.is_disconnected():
                    return
            except Exception:  # noqa: BLE001 — transport quirks never break streams
                pass

            # Heartbeat when the connection has been silent too long
            now = time.monotonic()
            if now - last_output >= heartbeat_interval:
                last_output = now
                yield ": keep-alive\n\n"

            # Relay wake OR poll tick → re-read the authoritative state
            frames = await _refresh()
            for frame in frames:
                last_output = time.monotonic()
                yield frame

            if all(_is_terminal(payload) for payload in last_sent.values()):
                return
    finally:
        logger.debug(
            "Processing stream closed (user=%s docs=%s)", user.id, document_ids
        )


@router.get(
    "/{document_id}/stream",
    summary="Stream one document's processing status (SSE)",
    description=(
        "Server-Sent Events feed for a single document: an initial `status` "
        "event followed by one event per state change, sourced from "
        "processing_jobs via the Redis relay with a slow poll re-read as the "
        "correctness fallback.  The connection closes once the document "
        "reaches READY or FAILED."
    ),
)
async def stream_document(
    document_id: str,
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.DOCUMENT_READ)),
) -> StreamingResponse:
    # Authorization + 404 BEFORE the stream opens
    await _status_of(db, user, document_id)
    return StreamingResponse(
        _stream_states(
            db=db,
            user=user,
            request=request,
            document_ids=[document_id],
            include_document_id=False,
        ),
        media_type=_MEDIA_TYPE,
        headers=_SSE_HEADERS,
    )


@router.get(
    "/stream",
    summary="Multiplexed processing stream for several documents (SSE)",
    description=(
        "One connection tracking N documents (the header processing "
        "indicator's transport — FE §11.2): `status` events carry the "
        "`document_id`; the connection closes when every tracked document "
        "is READY/FAILED.  Unknown/foreign ids fail BEFORE the stream opens."
    ),
)
async def stream_documents_multiplexed(
    request: Request,
    db: DbSession,
    user: User = Depends(require_permission(PermissionKey.DOCUMENT_READ)),
    ids: str = Query(
        ...,
        alias="ids",
        min_length=1,
        description="Comma-separated document IDs to track (max 50).",
        max_length=2000,
    ),
) -> StreamingResponse:
    document_ids = [chunk.strip() for chunk in ids.split(",") if chunk.strip()]
    if not document_ids:
        from app.core.exceptions import ValidationError

        raise ValidationError("At least one document id is required.")
    document_ids = list(dict.fromkeys(document_ids))[:50]
    # Authorize every id BEFORE the stream opens (bulk never bypasses
    # per-item authorization — Backend §46 rule 17).
    for document_id in document_ids:
        await _status_of(db, user, document_id)
    return StreamingResponse(
        _stream_states(
            db=db,
            user=user,
            request=request,
            document_ids=document_ids,
            include_document_id=True,
        ),
        media_type=_MEDIA_TYPE,
        headers=_SSE_HEADERS,
    )
