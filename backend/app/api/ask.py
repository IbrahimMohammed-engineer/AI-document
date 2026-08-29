"""
Ask API router — POST /ask (Phases 9–10).

The standalone RAG orchestration endpoint (pre-conversation form): runs
analyzer → rewriter → retrieval → context → generation → citation
extraction/validation and streams the answer as Server-Sent Events:

    event: token     data: {"text": "..."}       (zero or more)
    event: sources   data: {"sources": [...]}    (after generation + validation)
    event: done      data: {"groundedness": ..., "citations": [...], ...}
    event: error     data: {"code": "LLM_UNAVAILABLE", "message": "..."}

Contract notes (roadmap Phase 9–10 §APIs/§Error Handling):
  - Superseded by the conversation endpoint in Phase 11; retained for
    testing and the evaluation harness.
  - Insufficient evidence is a SUCCESS-shaped stream (sources: [], done
    with groundedness=ungrounded) — NOT an HTTP error (Backend §48).
  - Citations ride the terminal done payload (Phase 11 adds dedicated SSE
    ``citation`` events) and are emitted only AFTER generation + validation
    complete — never mid-stream (Backend §37).
  - The done payload's ``answer`` field is the post-validation text; when
    validation stripped claims it differs from the token stream and MUST
    replace it client-side (FE renders the validated text).
  - The assistant message + its citations persist ATOMICALLY in the
    pipeline (Backend §50) — done.message_id references the persisted row.
  - LLM_UNAVAILABLE (retries exhausted) and mid-stream provider drops are
    SSE ``error`` events; the user's question is preserved client-side.
  - Every call re-resolves the allowed document set against CURRENT
    permissions (Backend §46 rule 3) — the scope predicate is never
    broadened by client input.
  - Provider payloads (prompts/answers) are never audit-logged
    (Backend §54); stage latencies and token counts are (§55).

See:
  Backend-Architecture-Documentation.md §26 (RAG Architecture)
  Backend-Architecture-Documentation.md §35–36 (citation gen + validation)
  Backend-Architecture-Documentation.md §37 (Streaming — full SSE mechanics)
  roadmap Phase 9 steps 9–11; Phase 10 steps 7, 9
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
from app.schemas.ask import (
    AskCitationItem,
    AskDonePayload,
    AskRequest,
    AskSourceItem,
)
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
        message_id=outcome.message_id,
        answer=outcome.final_answer,
        citations=[AskCitationItem(**c.__dict__) for c in outcome.citations],
        model=outcome.model,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        intent=outcome.intent,
        topic=outcome.topic,
        used_rewrite=outcome.used_rewrite,
        used_reranker=outcome.used_reranker,
        regenerated=outcome.regenerated,
        stripped_claims=outcome.stripped_claims,
        entailment_checks=outcome.entailment_checks,
        latency_ms={
            "analyzer": outcome.timings.analyzer_ms,
            "rewrite": outcome.timings.rewrite_ms,
            "retrieval": outcome.timings.retrieval_ms,
            "context": outcome.timings.context_ms,
            "generation": outcome.timings.generation_ms,
            "citations": outcome.timings.citation_ms,
        },
    )
    return _sse("done", payload.model_dump(mode="json"))


def _error_event(code: str, message: str) -> str:
    return _sse("error", {"code": code, "message": message})


# ── POST /ask ─────────────────────────────────────────────────────────────────

@router.post(
    "/ask",
    summary="Ask a question over the document knowledge base (SSE stream)",
    description=(
        "Runs the full RAG pipeline — query analysis, conditional "
        "standalone-query rewriting, permission-aware hybrid retrieval, "
        "budgeted SOURCE-block context assembly, streamed generation, then "
        "citation extraction + claim validation — and returns the answer as "
        "Server-Sent Events: `token`* → `sources` → `done` (the done payload "
        "carries the validated `answer`, resolved `citations[]`, and "
        "`groundedness`).  When no evidence survives the retrieval threshold "
        "— or no claim survives citation validation — the stream is a "
        "successful `done` with `groundedness: \"ungrounded\"`; generation "
        "and persistence never ship an uncited answer."
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
