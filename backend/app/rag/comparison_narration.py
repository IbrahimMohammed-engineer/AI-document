"""
Comparison narration — LLM phrasing of already-classified changes (Phase 12).

Backend §41 principle: the narration LLM call does NOT decide what changed or
how severe it is — those decisions come from the deterministic pipeline
(§9.4/§9.5/§9.8).  The LLM here only phrases the structured result in
natural-language prose suitable for a business user.

Includes:
  - ``narrate_changes()``: constructs a natural-language summary from the
    ``comparison_changes`` rows already persisted in the DB.
  - ``parse_semantic_comparison()``: defensive parser for the per-section
    semantic-materiality classification call (§9.6) — reuses the same
    defensive-parsing pattern as ``query_analyzer.parse_analyzer_output``.

See PHASE-12-IMPLEMENTATION-PLAN.md §9.6, §14, §11 Task 9.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from app.infrastructure.llm import LLMMessage, LLMProvider, LLMProviderError
from app.rag.prompts import (
    CHANGE_NARRATION_SYSTEM_PROMPT,
    CHANGE_NARRATION_USER_TEMPLATE,
    SEMANTIC_COMPARISON_SYSTEM_PROMPT,
    SEMANTIC_COMPARISON_USER_TEMPLATE,
)

if TYPE_CHECKING:
    from app.models.comparison import ComparisonChange

logger = logging.getLogger(__name__)

# Token budget per side for semantic comparison (§9.6 — truncate before LLM call)
_SEMANTIC_TOKEN_BUDGET = 2_000
_FAST_TIMEOUT_SECONDS = 8.0


# ── Semantic comparison call (§9.6) ──────────────────────────────────────────

Materiality = Literal["material", "stylistic"]


class SemanticParseError(Exception):
    """The semantic comparison output was not the constrained JSON schema."""


def parse_semantic_comparison(raw: str) -> tuple[Materiality | None, str | None]:
    """Parse the per-section semantic comparison JSON output.

    Follows the exact defensive-parsing pattern from
    ``query_analyzer.parse_analyzer_output``:
      - Strip markdown fences, locate the JSON object.
      - Validate the ``materiality`` field is one of two allowed values.
      - On any failure: return (None, None) — never crash.

    Returns:
        (materiality, rationale) — both None on parse failure.
    """
    try:
        cleaned = _strip_to_json(raw)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return None, None
        materiality = data.get("materiality", "")
        if materiality not in ("material", "stylistic"):
            return None, None
        rationale = data.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            return materiality, rationale.strip()[:200]  # type: ignore[return-value]
        return materiality, None  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Semantic comparison parse failed: %s", exc)
        return None, None


async def classify_section_semantically(
    old_text: str,
    new_text: str,
    *,
    provider: LLMProvider,
    token_counter: object | None = None,
) -> tuple[Materiality | None, bool]:
    """Call the LLM to classify whether a section change is material or stylistic.

    Implements §9.6 failure-handling: any LLM error or parse failure returns
    (None, False) so the pipeline continues without semantic classification for
    this section — severity is then computed from proportion alone (§9.8 None
    branch).

    Args:
        old_text:      Section text from version A.
        new_text:      Section text from version B.
        provider:      Resolved LLM provider.
        token_counter: Optional tokenizer for budget enforcement.  If None,
                       a simple character-count proxy is used.

    Returns:
        (materiality, truncated) — materiality is None on any failure.
    """
    # Budget enforcement (§9.6): truncate to _SEMANTIC_TOKEN_BUDGET tokens each side
    old_clipped, new_clipped, truncated = _clip_to_budget(
        old_text, new_text, token_counter=token_counter
    )

    messages = [
        LLMMessage(role="system", content=SEMANTIC_COMPARISON_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=SEMANTIC_COMPARISON_USER_TEMPLATE.format(
                old_text=old_clipped,
                new_text=new_clipped,
            ),
        ),
    ]

    try:
        response = await provider.generate(
            messages,
            temperature=0.0,
            max_tokens=150,
            stream=False,
            timeout=_FAST_TIMEOUT_SECONDS,
        )
        materiality, _ = parse_semantic_comparison(response.content)
        return materiality, truncated
    except LLMProviderError as exc:
        logger.warning(
            "Semantic comparison LLM call failed (%s) — "
            "continuing without materiality for this section",
            exc.code,
        )
        return None, truncated
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Semantic comparison unexpected error (%s) — "
            "continuing without materiality for this section",
            exc,
        )
        return None, truncated


# ── Change narration (§14) ────────────────────────────────────────────────────

async def narrate_changes(
    changes: list[ComparisonChange],
    *,
    provider: LLMProvider,
) -> str:
    """Generate a natural-language summary of already-classified comparison changes.

    The LLM is strictly constrained to phrasing the supplied change list —
    it cannot invent new changes or alter severity (Backend §41).

    Args:
        changes: List of ``ComparisonChange`` ORM instances from
                 ``ComparisonRepository.list_changes()``.
        provider: Resolved LLM provider.

    Returns:
        Natural-language narration string.  Falls back to a structured plain-text
        summary if the LLM call fails — never raises.
    """
    if not changes:
        return "No changes were detected between the two versions."

    # Construct the structured CHANGES input for the prompt
    changes_data = []
    for c in changes:
        item: dict = {
            "severity": c.severity,
            "change_type": c.change_type,
            "section": c.section or "Unknown section",
        }
        if c.old_text:
            # Truncate for prompt safety
            item["old_text"] = c.old_text[:400]
        if c.new_text:
            item["new_text"] = c.new_text[:400]
        changes_data.append(item)

    changes_json = json.dumps(changes_data, indent=2)

    messages = [
        LLMMessage(role="system", content=CHANGE_NARRATION_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=CHANGE_NARRATION_USER_TEMPLATE.format(changes_json=changes_json),
        ),
    ]

    try:
        response = await provider.generate(
            messages,
            temperature=0.1,
            max_tokens=600,
            stream=False,
            timeout=15.0,
        )
        return response.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Change narration LLM call failed (%s) — using fallback", exc)
        return _fallback_narration(changes)


# ── Internal helpers ──────────────────────────────────────────────────────────

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


def _clip_to_budget(
    old_text: str,
    new_text: str,
    *,
    token_counter: object | None,
) -> tuple[str, str, bool]:
    """Clip old/new text to ~2,000 tokens each side.

    Uses the token_counter if provided; falls back to a 6-char-per-token
    approximation when none is available.
    """
    budget = _SEMANTIC_TOKEN_BUDGET

    def _token_count(text: str) -> int:
        if token_counter is not None and hasattr(token_counter, "count"):
            try:
                return token_counter.count(text)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return max(1, len(text) // 6)  # ~6 chars/token approximation

    def _clip(text: str) -> tuple[str, bool]:
        if _token_count(text) <= budget:
            return text, False
        # Clip by characters, using the approximation
        char_limit = budget * 6
        return text[:char_limit] + " [...]", True

    old_clipped, old_trunc = _clip(old_text)
    new_clipped, new_trunc = _clip(new_text)
    return old_clipped, new_clipped, old_trunc or new_trunc


def _fallback_narration(changes: list[ComparisonChange]) -> str:
    """Plain-text fallback narration when the LLM call fails."""
    from collections import defaultdict

    by_severity: dict[str, list] = defaultdict(list)
    for c in changes:
        by_severity[c.severity].append(c)

    lines: list[str] = ["Document comparison results:"]
    for sev in ("MAJOR", "MODERATE", "MINOR"):
        group = by_severity.get(sev, [])
        if group:
            lines.append(f"\n{sev} changes ({len(group)}):")
            for c in group:
                section = c.section or "Unknown section"
                lines.append(f"  • [{c.change_type}] {section}")
    return "\n".join(lines)
