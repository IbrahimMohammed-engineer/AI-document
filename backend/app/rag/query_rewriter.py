"""
Query rewriter — conversational standalone-query resolution (Phase 9).

Backend §28: embeddings and full-text search operate on the literal text
handed to them — a short conversational follow-up ("What about approval?")
retrieves poorly in isolation.  The rewriter resolves the last 2–3 turns
plus the current message into a single standalone retrieval query via a
fast constrained LLM call.

Stage policy (all Backend §28):
  - TRIGGER: a heuristic pre-check (prior context exists AND the message is
    short or demonstrably context-dependent) avoids paying an LLM call on
    self-contained questions.  A first message never needs rewriting.
  - DRIFT GUARD: the rewritten query is compared to the original message by
    embedding cosine similarity; below the floor the ORIGINAL message is
    used — a rewrite that hallucinated unrelated context is never trusted.
  - DEGRADATION: any provider/embedding failure falls back to the raw
    message.  This stage never fails the request.

DRIFT-GUARD INVARIANT (Backend §28 — the rule this module exists to
enforce): the rewritten query is a retrieval-INTERNAL artifact.  It is
never persisted as the user's words and never substituted into generation —
the generator receives the user's ORIGINAL message.

See:
  Backend-Architecture-Documentation.md §28 (Query Rewriting)
  Backend-Architecture-Documentation.md §51 (External Service Failures)
  roadmap Phase 9 steps 3, 11
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Sequence

from app.core.config import get_settings
from app.infrastructure.embeddings import EmbeddingProvider, get_embedding_provider
from app.infrastructure.llm import LLMMessage, LLMProvider, LLMProviderError
from app.rag.prompts import REWRITER_SYSTEM_PROMPT, REWRITER_USER_TEMPLATE

logger = logging.getLogger(__name__)

_FAST_TIMEOUT_SECONDS = 5.0  # Backend §51 — fast rewriting calls

# Demonstrative-pronoun / ellipsis markers that signal context dependence.
# Word-boundary anchored to avoid matching inside ordinary words.
_CONTEXT_MARKER_RE = re.compile(
    r"\b(?:it|its|itself|this|that|these|those|they|them|their|"
    r"aforementioned|same|above|earlier|previous|"
    r"what about|how about|and also|any of them|both)\b",
    re.IGNORECASE,
)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RewriteOutcome:
    """The retrieval query plus WHY it has that value.

    ``reason`` is logged/observed for Phase 18 evaluation:
      "no-history" | "self-contained" | "rewritten"
      | "fallback-similarity-floor" | "fallback-rewrite-error"
      | "fallback-embedding-error" | "fallback-no-llm"
    """

    retrieval_query: str
    used_rewrite: bool
    reason: str
    rewritten_text: str | None = None  # the drift-guarded candidate, when any


# ── Trigger heuristic (pure) ──────────────────────────────────────────────────

def should_rewrite(message: str, history_length: int) -> bool:
    """Heuristic pre-check — avoid an LLM call on self-contained questions.

    True only when prior context exists AND (the message is short relative
    to a conversational follow-up OR it carries demonstrative/ellipsis
    markers).  A first message in a conversation NEVER triggers rewriting
    (there is nothing to resolve against).
    """
    if history_length <= 0:
        return False
    settings = get_settings()
    word_count = len(message.split())
    if word_count <= settings.rewriter_trigger_max_words:
        return True
    return bool(_CONTEXT_MARKER_RE.search(message))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 on degenerate input (never raises)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _format_history(history: Sequence[LLMMessage]) -> str:
    """Render the bounded history for the rewriter prompt (last 2–3 turns)."""
    settings = get_settings()
    bounded = history[-(settings.llm_history_turns * 2):]
    lines = [
        f"{turn.role.capitalize()}: {turn.content.strip()}"
        for turn in bounded
        if turn.role in ("user", "assistant") and turn.content.strip()
    ]
    return "\n".join(lines) if lines else "(no prior turns)"


# ── Stage ─────────────────────────────────────────────────────────────────────

async def rewrite_query(
    message: str,
    history: Sequence[LLMMessage],
    *,
    provider: LLMProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> RewriteOutcome:
    """Produce the standalone retrieval query for the current message.

    Always returns a usable query — the raw message on any fallback.  The
    user's original words remain what generation receives and what will be
    persisted (Phase 11); only ``outcome.retrieval_query`` may differ.
    """
    settings = get_settings()

    if not should_rewrite(message, len(history)):
        reason = "no-history" if not history else "self-contained"
        return RewriteOutcome(retrieval_query=message, used_rewrite=False, reason=reason)

    from app.infrastructure.llm import get_llm_provider  # avoid import cycles

    llm = provider or get_llm_provider()
    if llm is None:
        return RewriteOutcome(
            retrieval_query=message, used_rewrite=False, reason="fallback-no-llm"
        )

    messages = [
        LLMMessage(role="system", content=REWRITER_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=REWRITER_USER_TEMPLATE.format(
                history=_format_history(history),
                message=message,
            ),
        ),
    ]

    try:
        response = await llm.generate(
            messages,
            temperature=0.0,
            max_tokens=200,
            stream=False,
            timeout=_FAST_TIMEOUT_SECONDS,
        )
    except LLMProviderError as exc:
        logger.warning(
            "Query rewriter: LLM call failed (%s) — using the raw message",
            exc.code,
        )
        return RewriteOutcome(
            retrieval_query=message, used_rewrite=False, reason="fallback-rewrite-error"
        )

    rewritten = response.content.strip().strip('"').strip()
    if not rewritten or rewritten.lower() == message.strip().lower():
        return RewriteOutcome(
            retrieval_query=message, used_rewrite=False, reason="self-contained"
        )

    # ── Drift guard: embedding similarity floor (Backend §28) ──────────────
    embedder = embedding_provider or get_embedding_provider()
    if embedder is None:
        # No embedding provider configured — accept the rewrite unguarded
        # (documented config state) rather than discarding useful resolution.
        logger.info("Query rewriter: no embedding provider — skipping drift guard")
        return RewriteOutcome(
            retrieval_query=rewritten, used_rewrite=True, reason="rewritten",
            rewritten_text=rewritten,
        )

    try:
        vectors = await embedder.embed([message, rewritten])
        similarity = _cosine(vectors[0], vectors[1])
    except Exception as exc:  # noqa: BLE001 — embedding outage must not fail the ask
        logger.warning(
            "Query rewriter: drift-guard embedding failed (%s) — using the raw message",
            exc,
        )
        return RewriteOutcome(
            retrieval_query=message, used_rewrite=False, reason="fallback-embedding-error"
        )

    if similarity < settings.rewriter_similarity_floor:
        logger.info(
            "Query rewriter: drift guard rejected rewrite (similarity %.2f < floor %.2f) "
            "— using the raw message",
            similarity, settings.rewriter_similarity_floor,
        )
        return RewriteOutcome(
            retrieval_query=message,
            used_rewrite=False,
            reason="fallback-similarity-floor",
            rewritten_text=rewritten,
        )

    logger.info(
        "Query rewriter: rewrote %r → %r (similarity %.2f)",
        message[:60], rewritten[:60], similarity,
    )
    return RewriteOutcome(
        retrieval_query=rewritten, used_rewrite=True, reason="rewritten",
        rewritten_text=rewritten,
    )
