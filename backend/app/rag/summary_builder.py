"""
Summary draft generation — one constrained-JSON LLM call (Phase 14).

``generate_summary_draft`` produces the schema-constrained summary draft
from an assembled ContextBundle.  NO citation logic lives here — validation
stays entirely in ``rag/citation_validator.py``, called by SummaryService.run
after this function returns (plan §5.8).

Failure policy mirrors ``query_analyzer.analyze_query`` exactly: malformed
output gets ONE constrained retry; a second failure raises
SummaryParseError (the job then fails cleanly rather than persisting a
garbage summary — plan §5.14).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.8.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.infrastructure.llm import LLMMessage, LLMProvider
from app.rag.context_builder import ContextBundle
from app.rag.prompts import (
    SUMMARY_PROMPT_VERSION,
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

_GENERATION_TIMEOUT_SECONDS = 30.0  # Backend §51 — generation-call budget

_JSON_FIELDS = (
    "executive_summary",
    "key_points",
    "dates",
    "roles",
    "requirements",
    "risks",
    "topics",
)


class SummaryParseError(Exception):
    """The summary output was not the constrained JSON schema (after retry)."""


@dataclass
class SummaryDraft:
    """The parsed summary draft.

    Every list item (except ``topics``) is raw text ending in one or more
    ``[N]`` markers — exactly like a chat sentence — so the same
    resolve_citations → validate_answer machinery that defends chat
    applies unchanged (plan §7.5).
    """

    executive_summary: str = ""
    key_points: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


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


def parse_summary_draft(raw: str) -> SummaryDraft:
    """Parse the constrained summary JSON output.

    Raises:
        SummaryParseError: On unparseable or schema-violating output —
            the caller retries once, then fails the job.
    """
    cleaned = _clean_json_text(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SummaryParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SummaryParseError("output is not a JSON object")

    def _string_list(key: str) -> list[str]:
        raw_list = data.get(key) or []
        if not isinstance(raw_list, list):
            return []
        return [
            item.strip()
            for item in raw_list
            if isinstance(item, str) and item.strip()
        ]

    executive = data.get("executive_summary")
    if executive is not None and not isinstance(executive, str):
        executive = None

    return SummaryDraft(
        executive_summary=(executive or "").strip(),
        key_points=_string_list("key_points"),
        dates=_string_list("dates"),
        roles=_string_list("roles"),
        requirements=_string_list("requirements"),
        risks=_string_list("risks"),
        topics=_string_list("topics"),
    )


async def generate_summary_draft(
    bundle: ContextBundle,
    document_name: str,
    version_label: str,
    provider: LLMProvider,
    *,
    extra_instruction: str | None = None,
) -> tuple[SummaryDraft, str | None, dict[str, int] | None]:
    """One constrained-JSON summary call with one retry on parse failure.

    Args:
        bundle: Assembled SOURCE-block context (build_context output).
        document_name: Display name for the user message.
        version_label: Human version label (falls back to the raw number).
        provider: Resolved LLM provider (never None — callers check).
        extra_instruction: Optional appended user-message instruction (the
            bounded regeneration pass reuses CITATION_EMPHASIS_INSTRUCTION
            here — mirroring AskService._regenerate_once, plan §5.7 step 8).

    Returns:
        (draft, model, token_usage) — token_usage is the provider-reported
        {prompt_tokens, completion_tokens} pair when available, else None.

    Raises:
        SummaryParseError: both attempts produced unparseable output.
        LLMProviderError: provider failure (worker retry policy applies).
    """
    user_content = SUMMARY_USER_TEMPLATE.format(
        context=bundle.prompt_text,
        document_name=document_name,
        version_label=version_label,
    )
    if extra_instruction:
        user_content = f"{user_content}{extra_instruction}"
    messages = [
        LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
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
            draft = parse_summary_draft(response.content)
            logger.info(
                "Summary builder: parsed draft (attempt %d) — exec=%d kp=%d "
                "dates=%d roles=%d reqs=%d risks=%d topics=%d prompt_version=%s",
                attempt,
                len(draft.executive_summary),
                len(draft.key_points),
                len(draft.dates),
                len(draft.roles),
                len(draft.requirements),
                len(draft.risks),
                len(draft.topics),
                SUMMARY_PROMPT_VERSION,
            )
            return draft, model, usage
        except SummaryParseError as exc:
            logger.warning(
                "Summary builder: malformed output (attempt %d/2): %s", attempt, exc
            )
            if attempt == 2:
                raise
            messages.append(LLMMessage(role="assistant", content=response.content))
            messages.append(LLMMessage(
                role="user",
                content=(
                    "That was not the required JSON schema. Reply with ONLY the "
                    "JSON object with keys executive_summary, key_points, dates, "
                    "roles, requirements, risks, topics."
                ),
            ))

    raise SummaryParseError("unreachable")  # keeps type-checkers honest


def _extract_usage(response: object) -> dict[str, int] | None:
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
