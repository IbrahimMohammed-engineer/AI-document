"""
Answer generation — prompt assembly + streamed LLM call (Phase 9).

Backend §34: combines the fixed, versioned system prompt, the assembled
SOURCE context, bounded recent history, and THE USER'S ORIGINAL MESSAGE
(never the rewritten retrieval query — Backend §28) into the
provider-appropriate message list, then streams the answer.

SECURITY — CENTRALIZED PROMPT CONSTRUCTION (Backend §53 item 3): this
module and ``rag/context_builder.py`` are the only code that assembles a
final prompt.  The generation call is pure text-in/text-out — the provider
is never given tools/function-calling (Backend §53 item 5), so even a
successful injection can only influence the next block of text, which
output validation (Phase 10) then checks.

Generation config (Backend §34): temperature 0.0–0.2 (factual grounding,
not creative generation), max tokens 600–1000 (cost + concise cited
answers), streamed by default.

Insufficient evidence (Backend §36/§48): zero chunks surviving the Phase 8
threshold means generation is NEVER attempted — the service raises
:class:`InsufficientEvidenceError`, which the API layer serializes as a
SUCCESSFUL response with ``groundedness: "ungrounded"``.  It is a typed
result, not an HTTP error: "I couldn't find enough information" is a valid
outcome of an honest RAG pipeline.

See:
  Backend-Architecture-Documentation.md §34 (LLM Integration)
  Backend-Architecture-Documentation.md §36 (insufficient-evidence path)
  Backend-Architecture-Documentation.md §53 (Prompt Injection Protection)
  roadmap Phase 9 steps 5–8
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator, Sequence

from app.core.config import get_settings
from app.infrastructure.llm import (
    LLMChunk,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
)
from app.rag.context_builder import ContextBundle
from app.rag.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


# ── Canary-token defense (Phase 16 plan §3.3B) ────────────────────────────────
#
# The system prompt plants the §CANARY-INJECTED sentinel in the INSTRUCTION
# channel and instructs the model to answer §CANARY-DETECTED if any SOURCE
# block contains it. A response carrying §CANARY-* therefore means a
# document hijacked the instruction channel: the contaminated sentence is
# stripped and the caller writes INJECTION_ATTEMPT_DETECTED (counts/model
# only — never prompt or answer text, Backend §54).
_CANARY_RE = re.compile(r"§CANARY-\w+")


def _detect_canary(text: str) -> bool:
    """True when the raw LLM response references the canary sentinel."""
    return bool(_CANARY_RE.search(text or ""))


def _strip_canary_sentences(text: str) -> str:
    """Remove sentences containing a §CANARY-* token from generated text."""
    if not _detect_canary(text):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not _CANARY_RE.search(s)]
    return " ".join(s.strip() for s in kept if s.strip()).strip()


def apply_canary_defense(text: str, *, model: str | None = None) -> tuple[str, bool]:
    """Post-generation canary defense. Returns (clean_text, detected).

    When the canary fires the contaminated sentence(s) are stripped and a
    security warning is logged (the AUDIT write happens in the service layer,
    which owns the DB session — generator stays session-free).
    """
    if not _detect_canary(text):
        return text, False
    logger.warning(
        "INJECTION ATTEMPT: canary sentinel echoed in generation output "
        "(model=%s) — contaminated sentence(s) stripped", model or "unknown",
    )
    return _strip_canary_sentences(text), True


# ── Insufficient evidence (typed, NOT an HTTP error — Backend §48) ────────────

class InsufficientEvidenceError(Exception):
    """Zero usable evidence survived retrieval's threshold.

    Serialized as a successful response with ``groundedness: "ungrounded"``
    — never a 4xx/5xx (Backend §48 note).  Generation is skipped entirely:
    an ungrounded attempt is never made (Backend §46 rule 8).
    """

    def __init__(
        self,
        message: str = (
            "I couldn't find enough information in the provided documents "
            "to answer this question."
        ),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.groundedness = "ungrounded"


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class GeneratedAnswer:
    """Complete generation result (non-streaming convenience path)."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    generation_ms: int = 0


# ── Prompt assembly (centralized — Backend §53) ───────────────────────────────

def build_generation_messages(
    context_bundle: ContextBundle,
    history: Sequence[LLMMessage],
    question: str,
    *,
    extra_instruction: str | None = None,
) -> list[LLMMessage]:
    """Assemble the final message list.

    Order: system instructions → bounded recent history → the user's
    message containing the SOURCE context + the ORIGINAL question.

    Args:
        context_bundle: Output of ``context_builder.build_context``.
        history:        Recent conversation turns (user/assistant) — trimmed
                        to the configured turn bound here (single place).
        question:       The user's ORIGINAL message — NOT the rewritten
                        retrieval query (Backend §28 drift-guard invariant).
        extra_instruction: Optional additional instruction appended to the
                        user message — used by the Phase 10 citation-emphasis
                        regeneration (one bounded retry, Backend §36).  Still
                        centralized HERE: no other code path assembles prompts.
    """
    settings = get_settings()
    bounded_history = list(history[-(settings.llm_history_turns * 2):])
    bounded_history = [
        m for m in bounded_history
        if m.role in ("user", "assistant") and m.content.strip()
    ]

    user_content = (
        f"The following SOURCE blocks are the evidence retrieved from the "
        f"organization's documents:\n\n"
        f"{context_bundle.prompt_text}\n\n"
        f"USER QUESTION\n{question}"
    )
    if extra_instruction:
        user_content += f"\n\n{extra_instruction}"

    return [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        *bounded_history,
        LLMMessage(role="user", content=user_content),
    ]


# ── Generation ────────────────────────────────────────────────────────────────

def _generation_params() -> dict:
    settings = get_settings()
    return dict(
        temperature=settings.llm_generation_temperature,
        max_tokens=settings.llm_generation_max_tokens,
    )


async def generate_answer(
    context_bundle: ContextBundle,
    history: Sequence[LLMMessage],
    question: str,
    *,
    provider: LLMProvider | None = None,
    extra_instruction: str | None = None,
) -> GeneratedAnswer:
    """Non-streaming generation (evaluation harness / Phase 10 regeneration).

    The regeneration path (Backend §36) is non-streaming by design: it is an
    internal bounded retry whose output replaces the streamed draft only
    after validation passes — a partial second stream to the client would
    duplicate text.
    """
    import time

    from app.infrastructure.llm import get_llm_provider

    llm = provider or get_llm_provider()
    if llm is None:
        raise LLMProviderError("No LLM provider configured", code="LLM_PROVIDER_UNAVAILABLE")

    messages = build_generation_messages(
        context_bundle, history, question, extra_instruction=extra_instruction
    )
    start = time.perf_counter()
    response = await llm.generate(messages, stream=False, **_generation_params())
    clean_text, _ = apply_canary_defense(response.content, model=response.model)
    return GeneratedAnswer(
        text=clean_text,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        generation_ms=int((time.perf_counter() - start) * 1000),
    )


async def stream_answer(
    context_bundle: ContextBundle,
    history: Sequence[LLMMessage],
    question: str,
    *,
    provider: LLMProvider | None = None,
    extra_instruction: str | None = None,
) -> AsyncIterator[LLMChunk]:
    """Stream generation as token deltas (the Ask AI default path).

    Token/cost capture (roadmap Phase 9 step 10): the caller accumulates
    the terminal chunk's usage and attributes it to the org + request —
    logged now, persisted with messages in Phase 11.

    ``extra_instruction`` (Phase 13): an optional deterministic app-supplied
    note appended to the user message (e.g. the inline conflict-disagreement
    acknowledgement hint, §20).  Prompt assembly stays centralized in
    ``build_generation_messages``.
    """
    from app.infrastructure.llm import get_llm_provider

    llm = provider or get_llm_provider()
    if llm is None:
        raise LLMProviderError("No LLM provider configured", code="LLM_PROVIDER_UNAVAILABLE")

    messages = build_generation_messages(
        context_bundle, history, question, extra_instruction=extra_instruction
    )
    async for chunk in llm.generate(messages, stream=True, **_generation_params()):
        yield chunk
