"""
Ask API router — POST /ask (Phase 9).

The standalone RAG orchestration endpoint (pre-conversation form): runs
analyzer → rewriter → retrieval → context → generation and streams the
answer as Server-Sent Events:

    event: token     data: {"text": "..."}       (zero or more)
    event: sources   data: {"sources": [...]}    (after generation completes)
    event: done      data: {"groundedness": "grounded" | "ungrounded", ...}
    event: error     data: {"code": "LLM_UNAVAILABLE", "message": "..."}

Contract notes (roadmap Phase 9 §APIs/§Error Handling):
  - Superseded by the conversation endpoint in Phase 11; retained for
    testing and the evaluation harness.
  - Insufficient evidence is a SUCCESS-shaped stream (sources: [], done
    with groundedness=ungrounded) — NOT an HTTP error (Backend §48).
  - LLM_UNAVAILABLE (retries exhausted) and mid-stream provider drops are
    SSE ``error`` events; the user's question is preserved client-side.
  - Every call re-resolves the allowed document set against CURRENT
    permissions (Backend §46 rule 3) — the scope predicate is never
    broadened by client input.
  - Provider payloads (prompts/answers) are never audit-logged
    (Backend §54); stage latencies and token counts are (§55).

See:
  Backend-Architecture-Documentation.md §26 (RAG Architecture)
  Backend-Architecture-Documentation.md §37 (Streaming — full SSE mechanics)
  roadmap Phase 9 steps 9–11
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.domain.versioning import VersionScope
from app.infrastructure.embeddings import EmbeddingProviderError
from app.infrastructure.llm import LLMMessage
from app.schemas.ask import AskDonePayload, AskRequest, AskSourceItem
from app.services.ask_service import AskService, AskStreamEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])

_MEDIA_TYPE = "text/event-stream"


# ── SSE formatting ────────────────────────────────────────────────────────────

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _token_event(e: AskStreamEvent) -> str:
    return _sse("token", {"text": e.text or ""})


def _sources_event(e: AskStreamEvent) -> str:
    sources = e.sources or []
    return _sse(
        "sources",
        {"sources": [AskSourceItem(**s.__dict__).model_dump() for s in sources]},
    )


def _done_event(e: AskStreamEvent) -> str:
    outcome = e.outcome
    assert outcome is not None
    payload = AskDonePayload(
        groundedness=outcome.groundedness,
        message=outcome.message,
        model=outcome.model,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        intent=outcome.intent,
        topic=outcome.topic,
        used_rewrite=outcome.used_rewrite,
        used_reranker=outcome.used_reranker,
        latency_ms={
            "analyzer": outcome.timings.analyzer_ms,
            "rewrite": outcome.timings.rewrite_ms,
            "retrieval": outcome.timings.retrieval_ms,
            "context": outcome.timings.context_ms,
            "generation": outcome.timings.generation_ms,
        },
    )
    return _sse("done", payload.model_dump())


def _error_event(code: str, message: str) -> str:
    return _sse("error", {"code": code, "message": message})


# ── POST /ask ─────────────────────────────────────────────────────────────────

@router.post(
    "/ask",
    summary="Ask a question over the document knowledge base (SSE stream)",
    description=(
        "Runs the full RAG pipeline — query analysis, conditional "
        "standalone-query rewriting, permission-aware hybrid retrieval, "
        "budgeted SOURCE-block context assembly, streamed generation — and "
        "returns the answer as Server-Sent Events: `token`* → `sources` → "
        "`done`.  When no evidence survives the retrieval threshold the "
        "stream is a successful `done` with `groundedness: \"ungrounded\"` "
        "and generation is never attempted."
    ),
)
async def ask(
    request: AskRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> StreamingResponse:
    """POST /ask — the first question→answer loop (M4)."""
    settings = get_settings()

    # ── Resolve the request scope (identical semantics to POST /search) ──
    scope: VersionScope
    if request.scope is None:
        scope = VersionScope.all_documents()
    elif request.scope.document_ids:
        scope = VersionScope.for_documents(request.scope.document_ids)
    elif request.scope.collection_ids:
        scope = VersionScope.for_collections(request.scope.collection_ids)
    else:
        scope = VersionScope.all_documents()

    if request.scope is not None and request.scope.as_of:
        scope = VersionScope(
            kind=scope.kind,
            document_ids=scope.document_ids,
            collection_ids=scope.collection_ids,
            as_of=request.scope.as_of,
        )

    # Bounded conversation context for the rewriter/generator (server-side
    # bound even though the schema already caps at 6).
    history = [
        LLMMessage(role=m.role, content=m.content)
        for m in (request.history or [])[-(settings.llm_history_turns * 2):]
    ]

    service = AskService(db)

    async def event_stream():
        try:
            async for event in service.ask_stream(
                request.question,
                current_user,
                scope=scope,
                history=history,
                top_k=request.top_k,
            ):
                if event.type == "token":
                    yield _token_event(event)
                elif event.type == "sources":
                    yield _sources_event(event)
                elif event.type == "done":
                    yield _done_event(event)
                elif event.type == "error":
                    yield _error_event(event.error_code or "LLM_UNAVAILABLE",
                                       event.error_message or "The AI service is unavailable.")
        except EmbeddingProviderError as exc:
            # Retrieval-stage embedding outage — typed, retryable (Backend §51)
            logger.error("Ask: embedding provider error (user=%s): %s",
                         current_user.id, exc)
            yield _error_event(
                "EMBEDDING_UNAVAILABLE",
                "The search service is temporarily unavailable. "
                "Please try again in a moment.",
            )
        except ExternalServiceError as exc:
            yield _error_event(exc.error_code, exc.message)
        except Exception:
            # Safety net — the safe envelope, never raw exception detail
            logger.exception("Ask: unhandled pipeline error (user=%s)", current_user.id)
            yield _error_event(
                "INTERNAL_ERROR",
                "An unexpected error occurred. Please try again or contact support.",
            )

    return StreamingResponse(
        event_stream(),
        media_type=_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering for SSE
        },
    )
