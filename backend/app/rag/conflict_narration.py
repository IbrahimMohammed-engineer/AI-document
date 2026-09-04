"""
Conflict narration — LLM phrasing of already-persisted conflicts (Phase 13).

Backend §42 / §41 philosophy: the narration LLM call does NOT detect
conflicts, decide severity, or alter anything — it only phrases the
already-persisted, already-authorized conflict list in natural language
("narrate, never originate").  The system prompt explicitly forbids
asserting any conflict not present in the structured input, so the
conversational answer can never fabricate a conflict (the task's
non-negotiable rule).

A deterministic fallback narration (no LLM) is provided so the
CONFLICT_DETECTION chat path degrades gracefully when no provider is
configured — mirroring ``comparison_narration._fallback_narration``.

See PHASE-13-IMPLEMENTATION-PLAN.md §19, §23 Task 12.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.infrastructure.llm import LLMMessage, LLMProvider
from app.rag.prompts import (
    CONFLICT_NARRATION_SYSTEM_PROMPT,
    CONFLICT_NARRATION_USER_TEMPLATE,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_NARRATION_TIMEOUT_SECONDS = 15.0


async def narrate_conflicts(
    conflicts: list[dict[str, Any]],
    *,
    provider: LLMProvider,
) -> str:
    """Generate natural-language prose from an already-fetched conflict list.

    Args:
        conflicts: Structured conflict dicts — each carrying topic, severity,
            detection_method, and per-statement document_name + statement_text
            (+ effective_date).  Built by ``ConflictService.list_for_chat``;
            contains ONLY conflicts the requesting user is authorized to see.
        provider:  Resolved LLM provider.

    Returns:
        Natural-language narration.  Falls back to the deterministic
        plain-text rendering if the LLM call fails — never raises.
    """
    if not conflicts:
        return "I didn't find any recorded conflicts in the documents you have access to."

    conflicts_json = json.dumps(conflicts, indent=2, default=str)

    messages = [
        LLMMessage(role="system", content=CONFLICT_NARRATION_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=CONFLICT_NARRATION_USER_TEMPLATE.format(conflicts_json=conflicts_json),
        ),
    ]

    try:
        response = await provider.generate(
            messages,
            temperature=0.1,
            max_tokens=600,
            stream=False,
            timeout=_NARRATION_TIMEOUT_SECONDS,
        )
        return response.content.strip()
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into chat
        logger.warning("Conflict narration LLM call failed (%s) — using fallback", exc)
        return fallback_conflict_narration(conflicts)


def fallback_conflict_narration(conflicts: list[dict[str, Any]]) -> str:
    """Plain-text deterministic rendering when the LLM call fails/unavailable."""
    if not conflicts:
        return "I didn't find any recorded conflicts in the documents you have access to."

    by_severity: dict[str, list[dict[str, Any]]] = {}
    for conflict in conflicts:
        by_severity.setdefault(str(conflict.get("severity", "MINOR")), []).append(conflict)

    lines: list[str] = ["I found the following recorded conflicts:"]
    for sev in ("MAJOR", "MODERATE", "MINOR"):
        group = by_severity.get(sev, [])
        if not group:
            continue
        for conflict in group:
            docs = ", ".join(
                sorted({s.get("document_name") or "Unknown document" for s in conflict.get("statements", [])})
            )
            lines.append(
                f"\n• [{sev}] {conflict.get('topic', 'Unlabelled conflict')} "
                f"— conflicting sources: {docs}"
            )
            for statement in conflict.get("statements", [])[:4]:
                doc = statement.get("document_name") or "Unknown document"
                text = (statement.get("statement_text") or "")[:200]
                lines.append(f"    - {doc}: \"{text}\"")
    return "\n".join(lines)
