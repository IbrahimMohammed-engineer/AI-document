"""
Query analyzer — intent classification + scope hints (Phase 9).

Backend §27: classifies the incoming question BEFORE retrieval because the
classification determines how retrieval and generation proceed.  V1 runs a
small, fast, structured-output LLM call — deliberately SEPARATE from the
answer-generation call ("figure out what the user wants" and "answer using
retrieved evidence" are different-shaped problems with different failure
modes).

Extracted per question:
  - intent:      QUESTION routes through standard RAG.  COMPARISON /
                 CHANGE_DETECTION / SUMMARY / CONFLICT_DETECTION are
                 classified now but route to their dedicated services in
                 Phases 12–14 — until those exist the pipeline logs and
                 proceeds as QUESTION (documented V1 behaviour).
  - temporal_scope: a structured hint ({year: 2025} / {relative: current})
                 consumed by metadata filtering for effective-date scoping.
  - scope_hints: document names/types the question explicitly mentions —
                 always ADVISORY.  The authoritative scope is the caller's
                 resolved permission scope; a mention never EXPANDS
                 retrieval beyond it (it can only narrow within it,
                 Backend §27/§29).
  - topic:       a short analytics/observability label — never used for
                 retrieval filtering (Backend §55).

Failure policy (Backend §51): the stage degrades gracefully — a malformed
output gets ONE constrained-schema retry, and any provider failure (or
second parse failure) falls back to the default analysis (intent=QUESTION,
no hints) with the raw query.  The request is never failed by this stage.

See:
  Backend-Architecture-Documentation.md §27 (Query Understanding)
  Backend-Architecture-Documentation.md §51 (External Service Failures)
  roadmap Phase 9 steps 2, 11
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from app.infrastructure.llm import LLMMessage, LLMProvider, LLMProviderError
from app.rag.prompts import ANALYZER_SYSTEM_PROMPT, ANALYZER_USER_TEMPLATE

logger = logging.getLogger(__name__)

QueryIntent = Literal[
    "QUESTION",
    "COMPARISON",
    "CHANGE_DETECTION",
    "SUMMARY",
    "CONFLICT_DETECTION",
    "EXTRACTION",
]

VALID_INTENTS: tuple[str, ...] = (
    "QUESTION", "COMPARISON", "CHANGE_DETECTION",
    "SUMMARY", "CONFLICT_DETECTION", "EXTRACTION",
)

# Intents whose dedicated services do not exist until Phases 12–14
DEFERRED_INTENTS: frozenset[str] = frozenset(
    {"COMPARISON", "CHANGE_DETECTION", "SUMMARY", "CONFLICT_DETECTION", "EXTRACTION"}
)

_FAST_TIMEOUT_SECONDS = 5.0  # Backend §51 — fast classification calls


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class QueryAnalysis:
    """Outcome of the analyzer stage."""

    intent: QueryIntent = "QUESTION"
    temporal_scope: dict | None = None
    scope_hints: list[str] = field(default_factory=list)
    topic: str | None = None
    # False when the stage degraded to the default (LLM failure / parse
    # failure) — logged so Phase 18's evaluation can quantify it.
    analyzer_used_llm: bool = False
    model: str | None = None

    @property
    def is_deferred_intent(self) -> bool:
        """True when the intent routes to a Phase 12–14 service."""
        return self.intent in DEFERRED_INTENTS


def default_analysis() -> QueryAnalysis:
    """The graceful-degradation outcome (Backend §51)."""
    return QueryAnalysis(intent="QUESTION", analyzer_used_llm=False)


# ── Parsing (pure — unit-testable in isolation) ───────────────────────────────

class AnalyzerParseError(Exception):
    """The analyzer output was not the constrained JSON schema."""


def _clean_json_text(raw: str) -> str:
    """Strip markdown fences / stray prose around a JSON object."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    brace = text.find("{")
    end = text.rfind("}")
    if brace != -1 and end != -1 and end > brace:
        text = text[brace : end + 1]
    return text


def _parse_temporal_scope(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    year = value.get("year")
    relative = value.get("relative")
    if isinstance(year, int):
        return {"year": year}
    if isinstance(year, str) and year.isdigit():
        return {"year": int(year)}
    if isinstance(relative, str) and relative.strip():
        return {"relative": relative.strip().lower()}
    return None


def parse_analyzer_output(raw: str) -> QueryAnalysis:
    """Parse the analyzer's constrained JSON output.

    Raises:
        AnalyzerParseError: On unparseable or schema-violating output —
            the caller retries once, then degrades to the default.
    """
    cleaned = _clean_json_text(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AnalyzerParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalyzerParseError("output is not a JSON object")

    intent = data.get("intent", "QUESTION")
    if not isinstance(intent, str) or intent.upper() not in VALID_INTENTS:
        raise AnalyzerParseError(f"unknown intent: {intent!r}")
    intent = intent.upper()  # type: ignore[assignment]

    scope_hints_raw = data.get("scope_hints") or []
    if not isinstance(scope_hints_raw, list):
        scope_hints_raw = []
    scope_hints = [
        h.strip() for h in scope_hints_raw
        if isinstance(h, str) and h.strip()
    ][:10]  # size-bounded — untrusted output must not balloon

    topic = data.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        topic = None
    else:
        topic = topic.strip()[:60]

    return QueryAnalysis(
        intent=intent,  # type: ignore[arg-type]
        temporal_scope=_parse_temporal_scope(data.get("temporal_scope")),
        scope_hints=scope_hints,
        topic=topic,
    )


# ── Stage ─────────────────────────────────────────────────────────────────────

async def analyze_query(
    query: str,
    *,
    provider: LLMProvider | None = None,
) -> QueryAnalysis:
    """Classify the query via a fast structured-output LLM call.

    Never raises for provider/parse failures — degrades to
    :func:`default_analysis` (Backend §51: classification failures skip the
    stage rather than failing the request).
    """
    from app.infrastructure.llm import get_llm_provider  # avoid import cycles

    llm = provider or get_llm_provider()
    if llm is None:
        logger.warning("Query analyzer: no LLM provider — using default analysis")
        return default_analysis()

    messages = [
        LLMMessage(role="system", content=ANALYZER_SYSTEM_PROMPT),
        LLMMessage(role="user", content=ANALYZER_USER_TEMPLATE.format(query=query)),
    ]

    for attempt in (1, 2):  # malformed output → one constrained retry
        try:
            response = await llm.generate(
                messages,
                temperature=0.0,
                max_tokens=200,
                stream=False,
                timeout=_FAST_TIMEOUT_SECONDS,
            )
            analysis = parse_analyzer_output(response.content)
        except AnalyzerParseError as exc:
            logger.warning(
                "Query analyzer: malformed output (attempt %d/2): %s", attempt, exc
            )
            if attempt == 2:
                return default_analysis()
            messages.append(LLMMessage(role="assistant", content=response.content))  # type: ignore[possibly-undefined]
            messages.append(LLMMessage(
                role="user",
                content=("That was not the required JSON schema. Reply with ONLY "
                         "the JSON object with keys intent, temporal_scope, "
                         "scope_hints, topic."),
            ))
            continue
        except LLMProviderError as exc:
            logger.warning(
                "Query analyzer: LLM call failed (%s) — using default analysis",
                exc.code,
            )
            return default_analysis()

        analysis.analyzer_used_llm = True
        analysis.model = response.model
        if analysis.is_deferred_intent:
            # Dedicated services arrive in Phases 12–14; until then the
            # standard RAG pipeline handles the question (logged so the
            # misrouting cost is measurable, Backend §27).
            logger.info(
                "Query analyzer: intent=%s routed to standard RAG "
                "(dedicated service arrives in a later phase)",
                analysis.intent,
            )
        logger.info(
            "Query analyzer: intent=%s topic=%s temporal=%s hints=%d",
            analysis.intent, analysis.topic, analysis.temporal_scope,
            len(analysis.scope_hints),
        )
        return analysis

    return default_analysis()  # unreachable — keeps type-checkers honest
