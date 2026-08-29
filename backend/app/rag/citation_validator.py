"""
Citation validation — claim extraction, reference checks, entailment (Phase 10).

Backend §36: the stage that makes "no hallucinated citations" and "prefer
honest uncertainty over confident fabrication" ENFORCED rather than
prompted-for:

    Generated answer (cleaned of invalid references by rag/citations.py)
       ↓ sentence segmentation (the documented V1 claim granularity)
       ↓ per factual sentence: carries ≥1 resolvable citation?  ──no──→ strip
       ↓                                                        (or regenerate
       │                                                         once, central)
       ↓ per claim–citation pair: entailment check (fast LLM) ──no──→ strip
       ↓ outcomes: grounded | partial (+ ungrounded upstream, no generation)

Policy (roadmap Phase 10 step 4 / Backend §36 outcomes):
  - Missing citation on a factual sentence → strip; when the uncited set is
    CENTRAL (> ``citation_regenerate_uncited_ratio`` of factual sentences),
    signal ONE bounded regeneration with citation emphasis — if the retry
    still fails, the sentences are dropped rather than shipped uncited.
  - Unsupported claim (entailment verdict ``no``) → strip, same policy.
  - Entailment verdict ``partial`` (related/consistent) → kept: borderline
    over-stripping is accepted but not sought; tracked in the outcome.
  - Entailment provider failure / budget exhaustion → the claim is NOT
    silently passed: it stays but groundedness degrades to ``partial`` and
    the degradation is logged (roadmap Phase 10 error handling).

The validator is synchronous and pure EXCEPT the entailment check, which is
injected as an async callable so unit tests substitute verdicts without any
LLM (roadmap Phase 10 testing: "validator decision logic given mocked
entailment results").

See:
  Backend-Architecture-Documentation.md §36 (Citation Validation)
  Backend-Architecture-Documentation.md §46 rules 6–8 (business rules)
  roadmap Phase 10 steps 4–5
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from app.core.config import get_settings
from app.rag.citations import (
    CitationExtraction,
    extract_references,
    split_sentences,
)
from app.rag.prompts import ENTAILMENT_SYSTEM_PROMPT, ENTAILMENT_USER_TEMPLATE

logger = logging.getLogger(__name__)

EntailmentVerdict = Literal["yes", "no", "partial"]
Groundedness = Literal["grounded", "partial", "ungrounded"]

_FAST_TIMEOUT_SECONDS = 5.0  # Backend §51 — fast classification calls

_CITATION_MARKER_RE = re.compile(r"\[(?:SOURCE\s*)?\d{1,2}\]")

# Transitional / meta sentences carry no checkable facts (Backend §36 —
# "excluding purely transitional/meta sentences like 'Here's what I found:'").
_TRANSITIONAL_RE = re.compile(
    r"^(here('s| is)|based on|according to the (provided |)source|"
    r"the (provided |)documents|these sources|i (couldn't|could not|found|"
    r"cannot|can't)|this (answer|information)|in summary|summary:|"
    r"note that|please note)",
    re.IGNORECASE,
)


# ── Claim model ───────────────────────────────────────────────────────────────

@dataclass
class Claim:
    """One factual sentence and the resolvable citations attached to it."""

    text: str
    start: int
    end: int
    citation_indices: list[int] = field(default_factory=list)
    # Set during validation:
    status: Literal["pending", "supported", "partial", "unsupported", "uncited", "unverified"] = "pending"


@dataclass
class ValidationOutcome:
    """Result of validating one answer against its citations."""

    final_text: str = ""
    groundedness: Groundedness = "grounded"
    # Per-claim decisions (the audit trail for Phase 18 metrics + tests)
    claims: list[Claim] = field(default_factory=list)
    stripped_claims: int = 0
    unsupported_claims: int = 0
    uncited_claims: int = 0
    unverified_claims: int = 0       # entailment failed/budget — kept, degraded
    entailment_checks: int = 0
    entailment_failures: int = 0     # provider errors (sustained → alert)
    # True when central claims failed and ONE regeneration is warranted
    should_regenerate: bool = False
    regeneration_reason: str = ""
    # True when validation stripped everything — service maps this to the
    # explicit insufficient-evidence response (success-shaped, Backend §48).
    emptied: bool = False


# ── Claim extraction (pure) ───────────────────────────────────────────────────

def _is_transitional(text: str) -> bool:
    return bool(_TRANSITIONAL_RE.match(text.strip()))


def extract_claims(answer_text: str) -> list[Claim]:
    """Segment the answer into factual sentence claims (Backend §36 step 1).

    Transitional/meta sentences are excluded from the citation requirement —
    they carry no checkable facts.  Citations attach to the sentence whose
    span contains the marker's start offset.
    """
    claims: list[Claim] = []
    for text, start, end in split_sentences(answer_text):
        claims.append(Claim(text=text, start=start, end=end))

    for match in extract_references(answer_text):
        for claim in claims:
            if claim.start <= match.start <= claim.end:
                claim.citation_indices.append(match.index)
                break

    for claim in claims:
        if _is_transitional(claim.text):
            claim.status = "supported"  # transitional — outside validation scope
    return claims


def find_central_regenerate_reason(claims: list[Claim]) -> str:
    """Decide whether the answer needs ONE citation-emphasis regeneration.

    Central failure (roadmap Phase 10 step 4: 'missing citation → strip
    (minor) or regenerate with citation emphasis (central, bounded to 1
    retry)'): more than ``citation_regenerate_uncited_ratio`` of factual
    claims are uncited — stripping would gut the answer, so regenerate once.
    """
    settings = get_settings()
    factual = [c for c in claims if not _is_transitional(c.text)]
    if not factual:
        return ""
    uncited = sum(1 for c in factual if not c.citation_indices)
    if uncited / len(factual) > settings.citation_regenerate_uncited_ratio:
        return f"{uncited}/{len(factual)} factual sentences lack citations"
    return ""


# ── Entailment check (the one I/O point — injectable) ─────────────────────────

EntailmentFn = Callable[[str, str], Awaitable[tuple[EntailmentVerdict | None, bool]]]
"""Async (claim, source_text) → (verdict | None, used_llm).

``verdict None`` means the check could not be completed (provider failure /
parse failure after retry) — the claim is treated as UNVERIFIED, never
silently passed (roadmap Phase 10 error handling).  ``used_llm`` reports
whether a real provider call happened (metrics).
"""


def _clean_json_text(raw: str) -> str:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    brace = text.find("{")
    end = text.rfind("}")
    if brace != -1 and end != -1 and end > brace:
        text = text[brace: end + 1]
    return text


async def check_entailment(
    claim: str,
    source_text: str,
    *,
    provider=None,
) -> tuple[EntailmentVerdict | None, bool]:
    """One fast structured entailment call (Backend §36).

    Returns ``(verdict, used_llm)``; ``verdict`` is None when the check could
    not be completed — one constrained-schema retry, then give up (the claim
    is downgraded to unverified by the caller, never silently passed).
    """
    if provider is None:
        from app.infrastructure.llm import get_llm_provider
        provider = get_llm_provider()
    if provider is None:
        return None, False

    from app.infrastructure.llm import LLMMessage

    messages = [
        LLMMessage(role="system", content=ENTAILMENT_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=ENTAILMENT_USER_TEMPLATE.format(
                source=source_text, claim=claim
            ),
        ),
    ]

    for attempt in (1, 2):  # one constrained-schema retry
        try:
            response = await provider.generate(
                messages,
                temperature=0.0,
                max_tokens=60,
                stream=False,
                timeout=_FAST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — provider raises broadly
            logger.warning(
                "Citation entailment check failed (attempt %d): %s", attempt, exc
            )
            return None, attempt == 1

        try:
            data = json.loads(_clean_json_text(response.content))
            verdict = str(data.get("verdict", "")).strip().lower()
            if verdict in ("yes", "no", "partial"):
                return verdict, True  # type: ignore[return-value]
        except (ValueError, AttributeError):
            pass
        logger.warning(
            "Citation entailment check returned unparseable output (attempt %d): %r",
            attempt, response.content[:120],
        )
        messages = [*messages]  # same prompt on the retry
    return None, True


# ── Validation (orchestrates the policy) ──────────────────────────────────────

async def validate_answer(
    extraction: CitationExtraction,
    *,
    entailment_fn: EntailmentFn | None = None,
    allow_regenerate: bool = True,
) -> ValidationOutcome:
    """Run the full validation pipeline over one extracted answer.

    Args:
        extraction: Output of ``citations.resolve_citations`` — the cleaned
            text (invalid references already stripped) plus resolved
            citations.  Citation objects supply the REAL source text for
            entailment (``quoted.text`` spans the actual chunk content).
        entailment_fn: Override for tests; defaults to ``check_entailment``.
        allow_regenerate: False on the post-regeneration re-validation pass —
            the loop is bounded to ONE retry regardless of outcome
            (Backend §36; cost/loop protection).
    """
    settings = get_settings()
    if not settings.citation_validation_enabled:
        return _passthrough_outcome(extraction)

    if entailment_fn is None:
        entailment_fn = check_entailment

    outcome = ValidationOutcome()
    claims = extract_claims(extraction.cleaned_text)
    outcome.claims = claims
    citation_by_index = {c.index: c for c in extraction.citations}

    # ── Reference check: uncited factual sentences ────────────────────────
    factual_claims = [c for c in claims if not _is_transitional(c.text)]
    transitional_claims = [c for c in claims if _is_transitional(c.text)]
    for claim in factual_claims:
        if not claim.citation_indices:
            claim.status = "uncited"
            outcome.uncited_claims += 1

    # Central failure → ONE bounded regeneration (decision only — the caller
    # regenerates and re-validates with allow_regenerate=False).
    if allow_regenerate:
        reason = find_central_regenerate_reason(claims)
        if reason:
            outcome.should_regenerate = True
            outcome.regeneration_reason = reason
            return outcome

    # ── Evidence verification: entailment per claim–citation pair ─────────
    budget = settings.citation_entailment_max_checks
    for claim in factual_claims:
        if claim.status != "pending":
            continue  # already uncited
        supported = False
        saw_partial = False
        # Strip markers from the claim text before the entailment call —
        # the verifier judges the SENTENCE, not its citation syntax.
        claim_text = _CITATION_MARKER_RE.sub("", claim.text).strip()
        for idx in claim.citation_indices:
            citation = citation_by_index.get(idx)
            if citation is None:
                continue  # defensive — markers are pre-resolved
            if outcome.entailment_checks >= budget:
                # Budget exhausted — keep the claim but NEVER count it as
                # verified (bounded cost, Backend §36).
                claim.status = "unverified"
                outcome.unverified_claims += 1
                break
            verdict, used_llm = await entailment_fn(claim_text, citation.quoted.text)
            if used_llm:
                outcome.entailment_checks += 1
            if verdict is None:
                outcome.entailment_failures += 1
                claim.status = "unverified"
                outcome.unverified_claims += 1
                break
            if verdict == "yes":
                supported = True
                claim.status = "supported"
                break
            if verdict == "partial":
                saw_partial = True
        else:
            # Every pair checked with a definite verdict; nothing said "yes".
            if claim.status == "pending":
                if saw_partial:
                    claim.status = "partial"      # related/consistent — kept
                    supported = True
                else:
                    claim.status = "unsupported"
                    outcome.unsupported_claims += 1
        if not supported and claim.status == "pending":
            claim.status = "unsupported"
            outcome.unsupported_claims += 1

    # ── Strip + outcome ────────────────────────────────────────────────────
    stripped_kinds = {"uncited", "unsupported"}
    kept_claims = [
        c for c in factual_claims if c.status not in stripped_kinds
    ]
    outcome.stripped_claims = sum(
        1 for c in factual_claims if c.status in stripped_kinds
    )

    # Transitional sentences always survive (they carry no facts).
    surviving = sorted([*kept_claims, *transitional_claims], key=lambda c: c.start)

    if factual_claims and not kept_claims:
        # Everything factual was stripped — nothing grounded remains.
        outcome.emptied = True
        outcome.final_text = ""
        outcome.groundedness = "ungrounded"
        return outcome

    outcome.final_text = _rebuild_text(extraction.cleaned_text, surviving)
    outcome.groundedness = _groundedness(outcome)
    _log_outcome(outcome)
    return outcome


# ── Helpers ───────────────────────────────────────────────────────────────────

def _passthrough_outcome(extraction: CitationExtraction) -> ValidationOutcome:
    """Validation disabled — Phase 9 behaviour (extract/resolve only)."""
    return ValidationOutcome(
        final_text=extraction.cleaned_text,
        groundedness="grounded",
    )


def _rebuild_text(original: str, surviving: list[Claim]) -> str:
    """Reconstruct the answer from surviving claims, preserving order.

    Runs on the CLEANED text (invalid markers already removed).  Any text
    outside claim spans (leading/trailing whitespace, blank lines) is
    collapsed — the validated answer is the joined surviving sentences.
    """
    parts = []
    for claim in surviving:
        original_slice = original[claim.start:claim.end]
        if original_slice.strip():
            parts.append(original_slice.strip())
    return " ".join(parts) if parts else ""


def _groundedness(outcome: ValidationOutcome) -> Groundedness:
    """grounded: everything passed; partial: anything stripped/unverified."""
    if (
        outcome.stripped_claims == 0
        and outcome.unverified_claims == 0
        and outcome.entailment_failures == 0
        and outcome.uncited_claims == 0
    ):
        return "grounded"
    return "partial"


def _log_outcome(outcome: ValidationOutcome) -> None:
    """Validation outcome logging — feeds Phase 18 quality metrics."""
    logger.info(
        "Citation validation: groundedness=%s claims=%d stripped=%d "
        "(uncited=%d unsupported=%d) unverified=%d entailment_checks=%d "
        "failures=%d regenerate=%s",
        outcome.groundedness, len(outcome.claims), outcome.stripped_claims,
        outcome.uncited_claims, outcome.unsupported_claims,
        outcome.unverified_claims, outcome.entailment_checks,
        outcome.entailment_failures, outcome.should_regenerate,
    )
