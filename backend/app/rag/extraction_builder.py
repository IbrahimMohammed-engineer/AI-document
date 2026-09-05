"""
Extraction draft generation — one constrained-JSON LLM call (Phase 14).

``generate_extraction_draft`` produces the four-category extraction draft
from an assembled ContextBundle.  NO citation logic lives here — validation
stays entirely in ``rag/citation_validator.py`` (plan §5.10).  Oversized
categories are truncated to ``extraction_max_items_per_category`` BEFORE
returning (untrusted output must not balloon persistence — mirrors
query_analyzer's scope_hints[:10] bounding pattern).

Failure policy mirrors ``summary_builder``: one constrained retry, then
ExtractionParseError.

See PHASE-14-IMPLEMENTATION-PLAN.md §5.10.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.infrastructure.llm import LLMMessage, LLMProvider
from app.rag.context_builder import ContextBundle
from app.rag.prompts import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

_GENERATION_TIMEOUT_SECONDS = 30.0  # Backend §51 — generation-call budget

_JSON_FIELDS = ("requirement", "risk", "date", "party")


class ExtractionParseError(Exception):
    """The extraction output was not the constrained JSON schema (after retry)."""


@dataclass
class ExtractionDraft:
    """The parsed extraction draft — four fixed categories, items ending in [N]."""

    requirement: list[str] = field(default_factory=list)
    risk: list[str] = field(default_factory=list)
    date: list[str] = field(default_factory=list)
    party: list[str] = field(default_factory=list)

    def items_for(self, category: str) -> list[str]:
        return getattr(self, category)


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


def parse_extraction_draft(
    raw: str, *, max_items_per_category: int
) -> ExtractionDraft:
    """Parse the constrained extraction JSON output, truncating oversized categories.

    Raises:
        ExtractionParseError: On unparseable or schema-violating output —
            the caller retries once, then fails the job.
    """
    import json

    cleaned = _clean_json_text(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionParseError("output is not a JSON object")

    def _string_list(key: str) -> list[str]:
        raw_list = data.get(key) or []
        if not isinstance(raw_list, list):
            return []
        cleaned_items = [
            item.strip()
            for item in raw_list
            if isinstance(item, str) and item.strip()
        ]
        return cleaned_items[:max_items_per_category]

    return ExtractionDraft(
        requirement=_string_list("requirement"),
        risk=_string_list("risk"),
        date=_string_list("date"),
        party=_string_list("party"),
    )


async def generate_extraction_draft(
    bundle: ContextBundle,
    provider: LLMProvider,
    *,
    max_items_per_category: int,
    extra_instruction: str | None = None,
) -> tuple[ExtractionDraft, str | None, dict[str, int] | None]:
    """One constrained-JSON extraction call with one retry on parse failure.

    Args:
        bundle: Assembled SOURCE-block context (build_context output).
        provider: Resolved LLM provider (never None — callers check).
        max_items_per_category: Defensive cap applied before returning.
        extra_instruction: Optional appended user-message instruction (the
            bounded regeneration pass, mirroring AskService._regenerate_once).

    Returns:
        (draft, model, token_usage).

    Raises:
        ExtractionParseError: both attempts produced unparseable output.
        LLMProviderError: provider failure (worker retry policy applies).
    """
    user_content = EXTRACTION_USER_TEMPLATE.format(context=bundle.prompt_text)
    if extra_instruction:
        user_content = f"{user_content}{extra_instruction}"
    messages = [
        LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]

    model: str | None = None
    usage: dict[str, int] | None = None
    for attempt in (1, 2):  # malformed output → one constrained retry
        response = await provider.generate(
            messages,
            temperature=0.0,
            max_tokens=2000,
            stream=False,
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
        model = response.model
        usage = _extract_usage(response)
        try:
            draft = parse_extraction_draft(
                response.content, max_items_per_category=max_items_per_category
            )
            logger.info(
                "Extraction builder: parsed draft (attempt %d) — req=%d risk=%d "
                "date=%d party=%d prompt_version=%s",
                attempt,
                len(draft.requirement),
                len(draft.risk),
                len(draft.date),
                len(draft.party),
                EXTRACTION_PROMPT_VERSION,
            )
            return draft, model, usage
        except ExtractionParseError as exc:
            logger.warning(
                "Extraction builder: malformed output (attempt %d/2): %s",
                attempt, exc,
            )
            if attempt == 2:
                raise
            messages.append(LLMMessage(role="assistant", content=response.content))
            messages.append(LLMMessage(
                role="user",
                content=(
                    "That was not the required JSON schema. Reply with ONLY the "
                    "JSON object with keys requirement, risk, date, party."
                ),
            ))

    raise ExtractionParseError("unreachable")  # keeps type-checkers honest


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Best-effort token-usage extraction (provider-dependent attribute)."""
    raw = getattr(response, "usage", None)
    usage = raw if isinstance(raw, dict) else getattr(raw, "__dict__", None)
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    return None
