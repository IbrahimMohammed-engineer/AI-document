"""
Ask service — the RAG orchestration (Phases 9–10).

Backend §26 Flow 4 (extended with the Phase 10 citation stage — vertical
slice M5):

    Question
      ↓ Query Analyzer   (small LLM, structured output; graceful fallback)
      ↓ Query Rewriter   (conditional; drift-guarded — retrieval-internal)
      ↓ Retrieval        (Phase 8: scope → hybrid → rerank → threshold)
      ↓ zero chunks?  → explicit insufficient-evidence response
      ↓                  (groundedness: ungrounded; NO generation call)
      Context Builder    (SOURCE 1..N labeled, budgeted, deduped)
      ↓ parallel map: source_index → chunk_id (backend-owned)
      Generator          (LLM, streamed, temp ≤ 0.2)
      ↓ Citation Extraction   — [N] markers resolved through the BACKEND's
      ↓                         source map, never model memory (§35)
      ↓ Citation Validation   — claims × citations entailment; strip /
      ↓                         regenerate-once policy (§36)
      ↓ groundedness: grounded | partial | ungrounded
      Atomic Persistence      — assistant message + citations rows in ONE
      ↓                         transaction (§50)
    SSE: token* → sources → done

Business rules enforced here (roadmap Phase 9–10 §Business Rules):
  - Per-message re-validation of scope against CURRENT permissions — every
    /ask call re-resolves the allowed set through the retriever (Backend
    §46 rule 3); a mention in the question never expands it.
  - The rewritten query is used ONLY for retrieval; the generator receives
    the user's original words.
  - Generation is skipped entirely when evidence is insufficient.
  - Citations are never fabricated: resolution comes from the context-
    assembly record; invalid references are stripped, never persisted.
  - A claim that cannot cite evidentially-supporting source text is
    stripped — or triggers ONE bounded regeneration with citation emphasis
    — never shipped uncited (Backend §36).
  - Citations and the assistant message are atomically persisted.
  - Token/cost capture on every generation call (including regeneration)
    — attributed to the org + request.
  - Latency instrumentation per stage.

See:
  Backend-Architecture-Documentation.md §26 (RAG Architecture)
  Backend-Architecture-Documentation.md §35–36 (citation gen + validation)
  Backend-Architecture-Documentation.md §50 (transaction boundaries)
  roadmap Phase 9 steps 9–11; Phase 10 steps 1–9
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import AsyncIterator, Awaitable, Callable, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.versioning import VersionScope
from app.infrastructure.llm import LLMMessage, get_llm_provider
from app.models.document import DocumentVersion
from app.models.user import User
from app.rag.citation_validator import (
    ValidationOutcome,
    validate_answer,
)
from app.rag.citations import CitationExtraction, ResolvedCitation, resolve_citations
from app.rag.context_builder import ContextBundle, build_context
from app.rag.generator import (
    InsufficientEvidenceError,
    generate_answer,
    stream_answer,
)
from app.rag.hybrid_search import HybridRetriever
from app.rag.prompts import CITATION_EMPHASIS_INSTRUCTION
from app.rag.query_analyzer import analyze_query
from app.rag.query_rewriter import rewrite_query
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.services.conflict_service import ConflictNotice, ConflictService

logger = logging.getLogger(__name__)

Groundedness = Literal["grounded", "partial", "ungrounded"]

# Callback consulted BETWEEN streamed chunks (Phase 11 — Backend §37):
# returns None to continue generation, or a stop reason label ("user" |
# "disconnect") to cancel — the partial text freezes as the final answer.
CancelCheck = Callable[[], Awaitable[str | None]]


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class AskSource:
    """One source attached to the answer.

    ``page_id`` anchors the citation navigation target (Phase 10); every
    other field round-trips from the context bundle unchanged (Backend §33).
    """

    index: int
    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str
    page_number: int
    section_title: str | None
    relevance: float
    snippet: str


@dataclass
class AskCitation:
    """One resolved, validated citation — the FE badge/popover payload.

    Everything the citation UI needs (FE §12 Citation type): exact quoted
    span + offsets for the highlight overlay, surrounding context for the
    preview popover, and the document/version/page/section navigation
    target.  All source fields come from the backend's own context map.
    """

    index: int
    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    version_number: int | None
    effective_date: date | None
    page_id: str
    page: int
    section: str | None
    text: str                    # quoted_text — REAL source text (Backend §35)
    char_start: int | None
    char_end: int | None
    context_before: str
    context_after: str
    relevance: float


@dataclass
class AskStageTimings:
    """Per-stage latency (ms) — logged now, formalized in Phase 19."""

    analyzer_ms: int = 0
    rewrite_ms: int = 0
    retrieval_ms: int = 0
    context_ms: int = 0
    generation_ms: int = 0
    citation_ms: int = 0         # Phase 10: extraction + validation (+ regen)


@dataclass
class AskOutcome:
    """Everything the API layer needs for the terminal ``done`` event."""

    question: str
    groundedness: Groundedness = "grounded"
    message: str | None = None          # set on the ungrounded path
    answer_text: str = ""               # raw streamed draft (Phase 9 compat)
    final_answer: str = ""              # post-validation text (the truth)
    sources: list[AskSource] = field(default_factory=list)
    citations: list[AskCitation] = field(default_factory=list)
    message_id: str | None = None       # persisted assistant row (atomic write)
    intent: str = "QUESTION"
    topic: str | None = None
    analyzer_used_llm: bool = False
    retrieval_query: str = ""
    used_rewrite: bool = False
    used_reranker: bool = False
    regenerated: bool = False
    stripped_claims: int = 0
    entailment_checks: int = 0
    stopped: bool = False                # Phase 11: frozen mid-generation
    stop_reason: str | None = None       # "user" | "disconnect"
    # Phase 13: deterministic inline conflict notices (§20) — populated
    # exclusively from the DB query over the retrieved chunk set, never
    # from LLM output, so the FE banner cannot be hallucinated.
    conflicts: list[ConflictNotice] = field(default_factory=list)
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timings: AskStageTimings = field(default_factory=AskStageTimings)


@dataclass
class AskStreamEvent:
    """One SSE event in the /ask response stream.

    ``type`` is one of:
      token           — {"text": delta}
      sources         — {"sources": [...]}   (after generation + validation)
      conflict_notice — {"conflicts": [{conflict_id, topic, severity}]}
                        (Phase 13, immediately after retrieval — a
                        deterministic DB query, never LLM output)
      done            — terminal, carries the AskOutcome summary incl. citations[]
      error           — mid-stream failure (LLM_UNAVAILABLE etc.); the user's
                        question is preserved client-side (persistence in Phase 11
                        makes this durable)

    Citations are NEVER streamed mid-generation: validity is unknowable
    until the complete answer exists and validation has run (Backend §37).
    """

    type: Literal["token", "sources", "conflict_notice", "done", "error"]
    text: str | None = None
    sources: list[AskSource] | None = None
    conflicts: list[ConflictNotice] | None = None
    outcome: AskOutcome | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class RegenerationResult:
    """Outcome of the single bounded citation-emphasis regeneration."""

    replaced: bool
    extraction: CitationExtraction | None = None
    validation: ValidationOutcome | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ── Service ───────────────────────────────────────────────────────────────────

class AskService:
    """Runs the full RAG + citation pipeline for one question (stateless)."""

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
        conversation_id: str | None = None,
        assistant_message_id: str | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> AsyncIterator[AskStreamEvent]:
        """Execute the pipeline, yielding stream events.

        Raises nothing for expected pipeline states — degraded stages and
        insufficient evidence are modeled as events.  Unexpected failures
        (embedding outage etc.) propagate to the API layer's error path.

        Phase 11 additions:
          - ``conversation_id``     persists the answer into the conversation
                                    (and bumps its ``updated_at`` recency
                                    cursor in the same transaction).
          - ``assistant_message_id`` pre-allocated UUID announced in the chat
                                    stream's ``start`` event — the persisted
                                    row reuses it so stop flags match.
          - ``should_cancel``       is consulted between streamed chunks; a
                                    non-None return freezes the partial text
                                    as the final answer (``stopped=True``)
                                    which still runs citation
                                    extraction/validation — complete
                                    sentences may remain citable
                                    (Backend §37 explicit-cancellation rule).
        """
        history = list(history or [])
        timings = AskStageTimings()
        llm = get_llm_provider()
        stopped = False
        stop_reason: str | None = None

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
        retrieval = await retriever.search(
            rewrite.retrieval_query,
            user,
            scope=scope,
            mode=self._settings.search_mode_default,
            top_k=top_k if top_k is not None else self._settings.rag_top_k_default,
        )
        timings.retrieval_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 3.5 (Phase 13): inline conflict surfacing (§20) ─────────
        # For EVERY intent: any OPEN conflict with >= 2 chunks among the
        # retrieved set means the retrieval itself surfaced disagreeing
        # evidence.  Deterministic DB query — no additional authorization
        # check needed BY DESIGN (retrieval is already permission-scoped).
        # Populated exclusively from the DB, never from LLM output.
        conflicts_among_sources: list[ConflictNotice] = []
        try:
            conflicts_among_sources = await ConflictService.find_conflicts_among_chunks(
                organization_id=user.organization_id,
                chunk_ids=[r.chunk_id for r in retrieval.results],
                db=self._db,
            )
        except Exception:  # noqa: BLE001 — surfacing must never fail the ask
            logger.exception("AskService: conflict-surfacing query failed (non-fatal)")
        if conflicts_among_sources:
            yield AskStreamEvent(
                type="conflict_notice", conflicts=conflicts_among_sources
            )

        def _partial_outcome() -> AskOutcome:
            outcome = AskOutcome(
                question=question,
                intent=analysis.intent,
                topic=analysis.topic,
                analyzer_used_llm=analysis.analyzer_used_llm,
                retrieval_query=rewrite.retrieval_query,
                used_rewrite=rewrite.used_rewrite,
                used_reranker=retrieval.used_reranker,
                model=llm.model_name if llm else None,
                timings=timings,
            )
            outcome.stopped = stopped
            outcome.stop_reason = stop_reason
            return outcome

        # ── Insufficient evidence: NO generation call (Backend §36) ───────
        if not retrieval.results:
            logger.info(
                "AskService: zero chunks survived the threshold — explicit "
                "insufficient-evidence response (no generation attempted)"
            )
            insufficiency = InsufficientEvidenceError()
            done = _partial_outcome()
            done.groundedness = "ungrounded"
            done.message = insufficiency.message
            done.final_answer = insufficiency.message
            await self._persist(
                done, user, citations=[],
                conversation_id=conversation_id, message_id=assistant_message_id,
            )
            yield AskStreamEvent(type="sources", sources=[])
            yield AskStreamEvent(type="done", outcome=done)
            self._log_request(done, user)
            return

        # ── Stage 4: context assembly ──────────────────────────────────────
        start = time.perf_counter()
        bundle = build_context(retrieval.results)
        timings.context_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 5: streamed generation ───────────────────────────────────
        # The USER'S ORIGINAL MESSAGE generates the answer — never the
        # rewritten retrieval query (Backend §28 drift-guard invariant).
        start = time.perf_counter()
        answer_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0

        try:
            # Phase 13 (§20): when the retrieved sources disagree, a short,
            # FULLY DETERMINISTIC app-supplied note joins the generation
            # context — only the model's prose acknowledgment is generative.
            conflict_instruction: str | None = None
            if conflicts_among_sources:
                topics = ", ".join(f"'{c.topic}'" for c in conflicts_among_sources[:3])
                conflict_instruction = (
                    "Note: the retrieved sources disagree on "
                    f"{topics}. Acknowledge this disagreement in your answer "
                    "without resolving it or picking a side."
                )
            async for chunk in stream_answer(
                bundle, history, question, extra_instruction=conflict_instruction
            ):
                # Cancellation checkpoint BETWEEN chunks (Backend §37):
                # abandoned/stopped requests stop costing money here.
                if should_cancel is not None:
                    cancel_reason = await should_cancel()
                    if cancel_reason:
                        stopped = True
                        stop_reason = cancel_reason
                        logger.info(
                            "AskService: generation cancelled (%s) after %d "
                            "chunk(s) (conversation=%s) — partial answer frozen",
                            cancel_reason, len(answer_parts), conversation_id,
                        )
                        break
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

        # Stopped before ANY token arrived: nothing to freeze or validate —
        # the question stays persisted (Phase 11 durability), no assistant
        # row is written (an empty answer is not an answer), and the done
        # event still terminates the stream with stopped=true.
        if stopped and not answer_text.strip():
            done = _partial_outcome()
            done.groundedness = "ungrounded"
            yield AskStreamEvent(type="sources", sources=[])
            yield AskStreamEvent(type="done", outcome=done)
            self._log_request(done, user)
            return

        # ── Stage 6 (Phase 10): citation extraction + validation ──────────
        start = time.perf_counter()
        extraction = resolve_citations(answer_text, bundle)
        validation = await validate_answer(extraction)
        regenerated = False

        # Central failure → ONE bounded regeneration with citation emphasis
        # (Backend §36; the loop is capped by citation_regeneration_max_retries).
        # A STOPPED answer never regenerates: the user asked to stop — the
        # strip policy alone applies to the frozen partial (complete
        # sentences may still be citable).
        if validation.should_regenerate and not stopped:
            result = await self._regenerate_once(
                bundle, history, question, validation.regeneration_reason
            )
            regenerated = result.replaced
            prompt_tokens += result.prompt_tokens
            completion_tokens += result.completion_tokens
            if result.replaced:
                assert result.extraction is not None and result.validation is not None
                extraction = result.extraction
                validation = result.validation
            # On regeneration failure the original stripped outcome stands
            # (claims dropped rather than shipped uncited).
        elif validation.should_regenerate and stopped:
            logger.info(
                "AskService: stopped answer skips regeneration — strip-only "
                "validation applies to the frozen partial"
            )

        if validation.emptied:
            # Nothing survived validation — the honest ungrounded outcome
            # (success-shaped, Backend §48), never an uncited answer.
            done = _partial_outcome()
            done.groundedness = "ungrounded"
            done.message = InsufficientEvidenceError().message
            done.final_answer = done.message
            done.answer_text = answer_text
            done.regenerated = regenerated
            done.stripped_claims = validation.stripped_claims
            done.entailment_checks = validation.entailment_checks
            done.prompt_tokens = prompt_tokens
            done.completion_tokens = completion_tokens
            timings.citation_ms = int((time.perf_counter() - start) * 1000)
            await self._persist(
                done, user, citations=[],
                conversation_id=conversation_id, message_id=assistant_message_id,
            )
            yield AskStreamEvent(type="sources", sources=[])
            yield AskStreamEvent(type="done", outcome=done)
            self._log_request(done, user)
            return

        timings.citation_ms = int((time.perf_counter() - start) * 1000)

        # ── Stage 7: assemble the outcome + citation payloads ─────────────
        done = _partial_outcome()
        done.groundedness = validation.groundedness
        done.answer_text = answer_text
        done.final_answer = validation.final_text
        done.conflicts = conflicts_among_sources
        done.sources = [
            AskSource(
                index=block.index,
                chunk_id=block.chunk_id,
                document_id=block.document_id,
                document_version_id=block.document_version_id,
                document_name=block.document_name,
                page_id=block.page_id,
                page_number=block.page_number,
                section_title=block.section_title,
                relevance=block.relevance,
                snippet=block.content[:500],
            )
            for block in bundle.blocks
        ]
        done.prompt_tokens = prompt_tokens
        done.completion_tokens = completion_tokens
        done.regenerated = regenerated
        done.stripped_claims = validation.stripped_claims
        done.entailment_checks = validation.entailment_checks

        version_info = await self._load_version_info(
            {c.document_version_id for c in extraction.citations}
        )
        done.citations = [
            self._to_ask_citation(c, version_info)
            for c in extraction.citations
        ]

        # ── Stage 8: atomic persistence (message + citations, ONE tx) ─────
        # Persists BEFORE the terminal event so the done payload carries the
        # real message_id — and AFTER validation, never mid-stream (§37).
        await self._persist(
            done, user, citations=extraction.citations,
            conversation_id=conversation_id, message_id=assistant_message_id,
        )

        yield AskStreamEvent(type="sources", sources=done.sources)
        yield AskStreamEvent(type="done", outcome=done)
        self._log_request(done, user)

    # ── Phase 10: regeneration + persistence + enrichment helpers ─────────

    async def _regenerate_once(
        self,
        bundle: ContextBundle,
        history: list[LLMMessage],
        question: str,
        reason: str,
    ) -> "RegenerationResult":
        """One citation-emphasis regeneration attempt (bounded — Backend §36).

        Non-streaming by design: the output replaces the streamed draft only
        if it passes re-validation (a partial second stream would duplicate
        text).  Any failure (provider unavailable, still-failing validation)
        leaves the original stripped outcome standing — claims are dropped
        rather than shipped uncited.

        Returns a :class:`RegenerationResult` whose token counts accumulate
        on the same answer's cost budget either way.
        """
        logger.info(
            "AskService: central citation failure (%s) — regeneration attempt 1/1",
            reason,
        )
        try:
            regenerated = await generate_answer(
                bundle,
                history,
                question,
                extra_instruction=CITATION_EMPHASIS_INSTRUCTION,
            )
        except Exception as exc:  # noqa: BLE001 — provider raises broadly
            logger.warning(
                "AskService: regeneration attempt failed (%s) — keeping the "
                "stripped original", exc,
            )
            return RegenerationResult(
                replaced=False,
                prompt_tokens=0,
                completion_tokens=0,
            )

        new_extraction = resolve_citations(regenerated.text, bundle)
        new_validation = await validate_answer(
            new_extraction, allow_regenerate=False
        )
        if new_validation.emptied or new_validation.should_regenerate:
            logger.info(
                "AskService: regenerated answer still failed citation "
                "validation — dropping it in favour of the stripped original"
            )
            return RegenerationResult(
                replaced=False,
                prompt_tokens=regenerated.prompt_tokens,
                completion_tokens=regenerated.completion_tokens,
            )

        return RegenerationResult(
            replaced=True,
            extraction=new_extraction,
            validation=new_validation,
            prompt_tokens=regenerated.prompt_tokens,
            completion_tokens=regenerated.completion_tokens,
        )

    def _merge_regen_tokens(self, done: AskOutcome) -> None:
        """(Removed — regeneration tokens are folded in at the call site.)"""

    async def _load_version_info(
        self, version_ids: set[str]
    ) -> dict[str, dict]:
        """Fetch version_number + effective_date for the cited versions.

        One query — these ride the citation payload (FE §12: the badge shows
        the version; effectiveDate is part of the Citation contract).
        """
        if not version_ids:
            return {}
        rows = await self._db.execute(
            select(
                DocumentVersion.id,
                DocumentVersion.version_number,
                DocumentVersion.effective_date,
            ).where(DocumentVersion.id.in_(version_ids))
        )
        return {
            row_id: {
                "version_number": version_number,
                "effective_date": effective_date,
            }
            for row_id, version_number, effective_date in rows.all()
        }

    @staticmethod
    def _to_ask_citation(
        citation: ResolvedCitation,
        version_info: dict[str, dict],
    ) -> AskCitation:
        info = version_info.get(citation.document_version_id, {})
        return AskCitation(
            index=citation.index,
            chunk_id=citation.chunk_id,
            document_id=citation.document_id,
            document_version_id=citation.document_version_id,
            document_name=citation.document_name,
            version_number=info.get("version_number"),
            effective_date=info.get("effective_date"),
            page_id=citation.page_id,
            page=citation.page_number,
            section=citation.section,
            text=citation.quoted.text,
            char_start=citation.quoted.char_start,
            char_end=citation.quoted.char_end,
            context_before=citation.quoted.context_before,
            context_after=citation.quoted.context_after,
            relevance=citation.relevance,
        )

    async def _persist(
        self,
        done: AskOutcome,
        user: User,
        *,
        citations: list[ResolvedCitation],
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Atomic assistant-message + citations write (Backend §50).

        A failure here NEVER corrupts the stream — the answer was already
        delivered; the write rolls back whole (atomicity means neither row
        exists) and the failure is logged for observability.  The done
        payload's citations still describe the answer (in-memory truth);
        only ``message_id`` stays unset.

        With ``conversation_id`` (Phase 11) the conversation's ``updated_at``
        recency cursor bumps INSIDE the same transaction (Flow 4 step 14).
        ``message_id`` reuses the pre-allocated UUID the stream announced.
        """
        repo = MessageRepository(self._db)
        message_metadata: dict | None = None
        if done.stopped:
            message_metadata = {"stopped": True, "stop_reason": done.stop_reason}
        try:
            message = await repo.save_assistant_message_with_citations(
                content=done.final_answer or (done.message or ""),
                groundedness=done.groundedness,
                citations=citations if done.groundedness != "ungrounded" else [],
                conversation_id=conversation_id,
                model=done.model,
                prompt_tokens=done.prompt_tokens or None,
                completion_tokens=done.completion_tokens or None,
                retrieval_ms=done.timings.retrieval_ms or None,
                latency_ms=(
                    done.timings.analyzer_ms + done.timings.rewrite_ms
                    + done.timings.retrieval_ms + done.timings.context_ms
                    + done.timings.generation_ms + done.timings.citation_ms
                ) or None,
                metadata=message_metadata,
                message_id=message_id,
                commit=conversation_id is None,
            )
            if conversation_id is not None:
                await ConversationRepository(self._db).bump_updated_at(conversation_id)
                await self._db.commit()
            done.message_id = message.id
        except Exception:
            logger.exception(
                "AskService: atomic message+citations persistence failed "
                "(org=%s user=%s conversation=%s) — neither row persisted",
                user.organization_id, user.id, conversation_id,
            )
            # The request-scoped session may be in a failed transaction
            # state after the rolled-back write; reset it so no further
            # session use (none expected) sees InvalidRequestError.
            await self._db.rollback()

    # ── Instrumentation ────────────────────────────────────────────────────

    def _log_request(self, outcome: AskOutcome, user: User) -> None:
        """Stage latencies + token/cost capture (roadmap Phase 9 steps 10–11).

        Provider payloads (prompts/answers) are NEVER logged here — they
        belong to tracing with different retention (Backend §54).
        """
        logger.info(
            "AskService: org=%s user=%s groundedness=%s intent=%s rewrite=%s "
            "reranker=%s sources=%d citations=%d stripped=%d entailment=%d "
            "regenerated=%s message_id=%s | latency_ms=%s (analyzer=%d "
            "rewrite=%d retrieval=%d context=%d generation=%d citations=%d) "
            "| tokens=%d+%d model=%s",
            user.organization_id, user.id, outcome.groundedness,
            outcome.intent, outcome.used_rewrite, outcome.used_reranker,
            len(outcome.sources), len(outcome.citations),
            outcome.stripped_claims, outcome.entailment_checks,
            outcome.regenerated, outcome.message_id,
            sum((outcome.timings.analyzer_ms, outcome.timings.rewrite_ms,
                 outcome.timings.retrieval_ms, outcome.timings.context_ms,
                 outcome.timings.generation_ms, outcome.timings.citation_ms)),
            outcome.timings.analyzer_ms, outcome.timings.rewrite_ms,
            outcome.timings.retrieval_ms, outcome.timings.context_ms,
            outcome.timings.generation_ms, outcome.timings.citation_ms,
            outcome.prompt_tokens, outcome.completion_tokens,
            outcome.model,
        )
