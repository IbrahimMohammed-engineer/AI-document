"""
Ask service — the RAG orchestration (Phase 9).

Backend §26 Flow 4 (the first question→answer loop, vertical slice M4):

    Question
      ↓ Query Analyzer   (small LLM, structured output; graceful fallback)
      ↓ Query Rewriter   (conditional; drift-guarded — retrieval-internal)
      ↓ Retrieval        (Phase 8: scope → hybrid → rerank → threshold)
      ↓ zero chunks?  → explicit insufficient-evidence response
      ↓                  (groundedness: ungrounded; NO generation call)
      Context Builder    (SOURCE 1..N labeled, budgeted, deduped)
      ↓ parallel map: source_index → chunk_id (backend-owned)
      Generator          (LLM, streamed, temp ≤ 0.2)
      ↓
    SSE: token* → sources → done

Business rules enforced here (roadmap Phase 9 §Business Rules):
  - Per-message re-validation of scope against CURRENT permissions — every
    /ask call re-resolves the allowed set through the retriever (Backend
    §46 rule 3); a mention in the question never expands it.
  - The rewritten query is used ONLY for retrieval; the generator receives
    the user's original words.
  - Generation is skipped entirely when evidence is insufficient.
  - Token/cost capture on every generation call — attributed to the org +
    request and logged (persisted with messages in Phase 11).
  - Latency instrumentation per stage — logged now, formalized in Phase 19.

See:
  Backend-Architecture-Documentation.md §26 (RAG Architecture)
  Backend-Architecture-Documentation.md §36/§48 (insufficient evidence)
  roadmap Phase 9 steps 9–11
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.versioning import VersionScope
from app.infrastructure.llm import LLMMessage, get_llm_provider
from app.models.user import User
from app.rag.context_builder import build_context
from app.rag.generator import InsufficientEvidenceError, stream_answer
from app.rag.hybrid_search import HybridRetriever
from app.rag.query_analyzer import analyze_query
from app.rag.query_rewriter import rewrite_query

logger = logging.getLogger(__name__)

Groundedness = Literal["grounded", "partial", "ungrounded"]


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class AskSource:
    """One source attached to the answer.

    Phase 9 ships the source LIST; citation objects (quoted spans,
    char offsets, validation) arrive with Phase 10.  Every field here
    round-trips from the context bundle unchanged (Backend §33).
    """

    index: int
    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_number: int
    section_title: str | None
    relevance: float
    snippet: str


@dataclass
class AskStageTimings:
    """Per-stage latency (ms) — logged now, formalized in Phase 19."""

    analyzer_ms: int = 0
    rewrite_ms: int = 0
    retrieval_ms: int = 0
    context_ms: int = 0
    generation_ms: int = 0


@dataclass
class AskOutcome:
    """Everything the API layer needs for the terminal ``done`` event."""

    question: str
    groundedness: Groundedness = "grounded"
    message: str | None = None          # set on the ungrounded path
    answer_text: str = ""
    sources: list[AskSource] = field(default_factory=list)
    intent: str = "QUESTION"
    topic: str | None = None
    analyzer_used_llm: bool = False
    retrieval_query: str = ""
    used_rewrite: bool = False
    used_reranker: bool = False
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timings: AskStageTimings = field(default_factory=AskStageTimings)


@dataclass
class AskStreamEvent:
    """One SSE event in the /ask response stream.

    ``type`` is one of:
      token   — {"text": delta}
      sources — {"sources": [...]}   (after generation completes)
      done    — terminal, carries the AskOutcome summary
      error   — mid-stream failure (LLM_UNAVAILABLE etc.); the user's
                question is preserved client-side (persistence in Phase 11
                makes this durable)
    """

    type: Literal["token", "sources", "done", "error"]
    text: str | None = None
    sources: list[AskSource] | None = None
    outcome: AskOutcome | None = None
    error_code: str | None = None
    error_message: str | None = None


# ── Service ───────────────────────────────────────────────────────────────────

class AskService:
    """Runs the full basic RAG pipeline for one question (stateless)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._settings = get_settings()

    async def ask_stream(
        self,
        question: str,
        user: User,
        *,
        scope: VersionScope | None = None,
        history: Sequence[LLMMessage] | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[AskStreamEvent]:
        """Execute the pipeline, yielding stream events.

        Raises nothing for expected pipeline states — degraded stages and
        insufficient evidence are modeled as events.  Unexpected failures
        (embedding outage etc.) propagate to the API layer's error path.
        """
        history = list(history or [])
        timings = AskStageTimings()
        llm = get_llm_provider()

        # ── Stage 1: query analyzer (graceful degradation inside) ─────────
        start = time.perf_counter()
        analysis = await analyze_query(question)
        timings.analyzer_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 2: query rewriter (conditional + drift guard) ───────────
        start = time.perf_counter()
        rewrite = await rewrite_query(question, history)
        timings.rewrite_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 3: retrieval (Phase 8 pipeline — scope re-resolved HERE,
        #    per message, against current permissions; Backend §46 rule 3) ─
        start = time.perf_counter()
        retriever = HybridRetriever(self._db)
        outcome = await retriever.search(
            rewrite.retrieval_query,
            user,
            scope=scope,
            mode=self._settings.search_mode_default,
            top_k=top_k if top_k is not None else self._settings.rag_top_k_default,
        )
        timings.retrieval_ms = int((time.perf_counter() - start) * 1000)

        def _partial_outcome() -> AskOutcome:
            return AskOutcome(
                question=question,
                intent=analysis.intent,
                topic=analysis.topic,
                analyzer_used_llm=analysis.analyzer_used_llm,
                retrieval_query=rewrite.retrieval_query,
                used_rewrite=rewrite.used_rewrite,
                used_reranker=outcome.used_reranker,
                model=llm.model_name if llm else None,
                timings=timings,
            )

        # ── Insufficient evidence: NO generation call (Backend §36) ───────
        if not outcome.results:
            logger.info(
                "AskService: zero chunks survived the threshold — explicit "
                "insufficient-evidence response (no generation attempted)"
            )
            insufficiency = InsufficientEvidenceError()
            done = _partial_outcome()
            done.groundedness = "ungrounded"
            done.message = insufficiency.message
            yield AskStreamEvent(type="sources", sources=[])
            yield AskStreamEvent(type="done", outcome=done)
            self._log_request(done, user)
            return

        # ── Stage 4: context assembly ──────────────────────────────────────
        start = time.perf_counter()
        bundle = build_context(outcome.results)
        timings.context_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 5: streamed generation ───────────────────────────────────
        # The USER'S ORIGINAL MESSAGE generates the answer — never the
        # rewritten retrieval query (Backend §28 drift-guard invariant).
        start = time.perf_counter()
        answer_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0

        try:
            async for chunk in stream_answer(bundle, history, question):
                if chunk.delta:
                    answer_parts.append(chunk.delta)
                    yield AskStreamEvent(type="token", text=chunk.delta)
                if chunk.finish_reason == "usage":
                    prompt_tokens = chunk.prompt_tokens or 0
                    completion_tokens = chunk.completion_tokens or 0
        except Exception as exc:
            # Mid-stream provider drop → SSE error event; retries for
            # transient failures already happened at the provider boundary.
            logger.error("AskService: generation failed mid-stream: %s", exc)
            yield AskStreamEvent(
                type="error",
                error_code="LLM_UNAVAILABLE",
                error_message=(
                    "The AI service stopped responding while generating the "
                    "answer. Your question is preserved — please try again."
                ),
            )
            return
        finally:
            timings.generation_ms = int((time.perf_counter() - start) * 1000)

        answer_text = "".join(answer_parts)

        done = _partial_outcome()
        done.groundedness = "grounded"
        done.answer_text = answer_text
        done.sources = [
            AskSource(
                index=block.index,
                chunk_id=block.chunk_id,
                document_id=block.document_id,
                document_version_id=block.document_version_id,
                document_name=block.document_name,
                page_number=block.page_number,
                section_title=block.section_title,
                relevance=block.relevance,
                snippet=block.content[:500],
            )
            for block in bundle.blocks
        ]
        done.prompt_tokens = prompt_tokens
        done.completion_tokens = completion_tokens

        yield AskStreamEvent(type="sources", sources=done.sources)
        yield AskStreamEvent(type="done", outcome=done)
        self._log_request(done, user)

    # ── Instrumentation ────────────────────────────────────────────────────

    def _log_request(self, outcome: AskOutcome, user: User) -> None:
        """Stage latencies + token/cost capture (roadmap Phase 9 steps 10–11).

        Provider payloads (prompts/answers) are NEVER logged here — they
        belong to tracing with different retention (Backend §54).
        """
        logger.info(
            "AskService: org=%s user=%s groundedness=%s intent=%s rewrite=%s "
            "reranker=%s sources=%d | latency_ms=%s (analyzer=%d rewrite=%d "
            "retrieval=%d context=%d generation=%d) | tokens=%d+%d model=%s",
            user.organization_id, user.id, outcome.groundedness,
            outcome.intent, outcome.used_rewrite, outcome.used_reranker,
            len(outcome.sources),
            sum((outcome.timings.analyzer_ms, outcome.timings.rewrite_ms,
                 outcome.timings.retrieval_ms, outcome.timings.context_ms,
                 outcome.timings.generation_ms)),
            outcome.timings.analyzer_ms, outcome.timings.rewrite_ms,
            outcome.timings.retrieval_ms, outcome.timings.context_ms,
            outcome.timings.generation_ms,
            outcome.prompt_tokens, outcome.completion_tokens,
            outcome.model,
        )
