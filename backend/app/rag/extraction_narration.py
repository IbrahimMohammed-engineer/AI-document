"""
Extraction narration — LLM phrasing of already-persisted items (Phase 14).

Mirrors ``conflict_narration.py``'s narrate/fallback pair exactly: one LLM
call instructed to phrase ONLY the provided items (never invent new ones —
same "narrate, never originate" prompt discipline), with a deterministic
template fallback when no provider is configured or the call fails.

See PHASE-14-IMPLEMENTATION-PLAN.md §5.11.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Sequence

from app.infrastructure.llm import LLMMessage, LLMProvider
from app.rag.prompts import (
    EXTRACTION_NARRATION_SYSTEM_PROMPT,
    EXTRACTION_NARRATION_USER_TEMPLATE,
)

if TYPE_CHECKING:
    from app.models.extraction import DocumentExtractionItem

logger = logging.getLogger(__name__)

_CATEGORY_HEADINGS = {
    "requirement": "Requirements",
    "risk": "Risks",
    "date": "Dates",
    "party": "Parties",
}


async def narrate_extraction(
    items: Sequence[DocumentExtractionItem],
    *,
    provider: LLMProvider,
) -> str:
    """Generate prose over already-validated extraction items (one LLM call).

    Falls back to the deterministic template on any provider failure —
    never raises (identical contract to narrate_conflicts).
    """
    if not items:
        return "No items were extracted from this document."

    items_data = [
        {
            "category": item.category,
            "label": (item.label or "")[:400],
        }
        for item in items
    ]

    messages = [
        LLMMessage(role="system", content=EXTRACTION_NARRATION_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=EXTRACTION_NARRATION_USER_TEMPLATE.format(
                items_json=json.dumps(items_data, indent=2)
            ),
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
    except Exception as exc:  # noqa: BLE001 — narration never breaks the stream
        logger.warning(
            "Extraction narration LLM call failed (%s) — using fallback", exc
        )
        return _fallback_extraction_narration(items)


def _fallback_extraction_narration(items: Sequence[DocumentExtractionItem]) -> str:
    """Deterministic grouped-by-category rendering (no LLM)."""
    if not items:
        return "No items were extracted from this document."

    lines: list[str] = ["Here is what I found in this document:"]
    by_category: dict[str, list[Any]] = {}
    for item in items:
        by_category.setdefault(item.category, []).append(item)

    for category in ("requirement", "risk", "date", "party"):
        group = by_category.get(category, [])
        if not group:
            continue
        heading = _CATEGORY_HEADINGS.get(category, category.title())
        lines.append("")
        lines.append(f"**{heading}:**")
        lines.extend(f"- {item.label}" for item in group)
    return "\n".join(lines)
