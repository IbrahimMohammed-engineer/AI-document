"""
Contradiction-check output parsing (Phase 13).

``parse_contradiction_check`` implements §11's defensive output contract for
the contradiction-check LLM call — the exact parsing pattern
``query_analyzer.parse_analyzer_output`` and
``comparison_narration.parse_semantic_comparison`` establish:

  - Strip markdown fences / stray prose around the JSON object.
  - Validate types; CLAMP out-of-range confidence rather than reject.
  - Truncate the topic label to a bounded length.
  - On ANY failure return None — never raise into the scan loop.

The parsed result only ever GATES whether ``ConflictService`` calls its own
deterministic ``persist_or_merge`` — the LLM never writes to the database and
never decides severity, effective-date state, or deduplication (§11's
non-negotiable rule).

See PHASE-13-IMPLEMENTATION-PLAN.md §11, §23 Task 6.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.domain.conflict_rules import CONFLICT_TOPIC_MAX_CHARS

logger = logging.getLogger(__name__)


@dataclass
class ContradictionResult:
    """One parsed contradiction-check verdict (gates persistence only)."""

    is_conflict: bool
    confidence: float
    reason: str | None = None
    conflict_topic: str | None = None


def _strip_to_json(raw: str) -> str:
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


def parse_contradiction_check(raw: str) -> ContradictionResult | None:
    """Parse the contradiction-check LLM output defensively.

    Validation rules (§11):
      - ``is_conflict`` must be a bool (``true``/``false`` JSON literals).
      - ``confidence`` must parse as a float in [0, 1]; out-of-range values
        are clamped, not rejected.
      - ``conflict_topic`` is truncated to CONFLICT_TOPIC_MAX_CHARS.
      - ``reason`` is an optional bounded string.

    Returns:
        The parsed ContradictionResult, or None on any parse/schema failure
        — the caller treats None as "candidate skipped, scan continues".
    """
    try:
        cleaned = _strip_to_json(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("Contradiction check parse failed (not JSON): %s", exc)
        return None
    if not isinstance(data, dict):
        logger.debug("Contradiction check parse failed: output is not an object")
        return None

    is_conflict = data.get("is_conflict")
    if not isinstance(is_conflict, bool):
        logger.debug("Contradiction check parse failed: is_conflict not a bool")
        return None

    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        logger.debug("Contradiction check parse failed: confidence not numeric")
        return None
    confidence = max(0.0, min(1.0, float(confidence)))  # clamp, never reject

    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        reason = reason.strip()[:300]
    else:
        reason = None

    topic = data.get("conflict_topic")
    if isinstance(topic, str) and topic.strip():
        topic = topic.strip()[:CONFLICT_TOPIC_MAX_CHARS]
    else:
        topic = None

    return ContradictionResult(
        is_conflict=is_conflict,
        confidence=confidence,
        reason=reason,
        conflict_topic=topic,
    )
