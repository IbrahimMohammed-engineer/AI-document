"""
Unit tests — citation validation decision logic (Phase 10, Backend §36).

Covers (roadmap Phase 10 §Testing): "validator decision logic given mocked
entailment results — missing/invalid/unsupported/low-confidence → correct
accept/strip/regenerate decisions", plus the entailment output parser and
the provider-failure degradation (unverified → partial, never silently
passed).

Pure logic — the entailment check is injected, so no LLM and no Docker.
"""
from __future__ import annotations

import pytest

from app.rag.citation_validator import (
    _clean_json_text,
    extract_claims,
    find_central_regenerate_reason,
    validate_answer,
)
from app.rag.citations import ResolvedCitation, QuotedSpan
from app.rag.context_builder import ContextBundle, SourceBlock


# ── Builders ──────────────────────────────────────────────────────────────────

def _citation(index: int, source_text: str) -> ResolvedCitation:
    return ResolvedCitation(
        index=index,
        chunk_id=f"chunk-{index}",
        document_id=f"doc-{index}",
        document_version_id=f"ver-{index}",
        document_name=f"Document {index}",
        page_id=f"page-{index}",
        page_number=index,
        section=f"{index}.0 Section",
        relevance=0.8,
        quoted=QuotedSpan(
            text=source_text, char_start=0, char_end=len(source_text)
        ),
    )


def _bundle(*blocks: SourceBlock) -> ContextBundle:
    return ContextBundle(blocks=list(blocks))


def _extraction(answer: str, *citations: ResolvedCitation):
    from app.rag.citations import CitationExtraction
    return CitationExtraction(
        citations=list(citations),
        cleaned_text=answer,
        invalid_indices=[],
    )


async def _yes(claim: str, source: str):
    return "yes", True


async def _no(claim: str, source: str):
    return "no", True


async def _partial(claim: str, source: str):
    return "partial", True


async def _broken(claim: str, source: str):
    return None, True  # provider/parse failure


# ── Claim extraction ──────────────────────────────────────────────────────────

@pytest.mark.unit
class TestExtractClaims:

    def test_markers_attach_to_containing_sentence(self):
        claims = extract_claims("Fact one lives here. [1] Fact two. [2]")
        factual = [c for c in claims if c.citation_indices]
        assert [(c.citation_indices, c.text[:4]) for c in factual] == [
            ([1], "Fact"),
            ([2], "Fact"),
        ]

    def test_transitional_sentences_exempt(self):
        claims = extract_claims(
            "Here's what I found: the approval process has four stages. [1]"
        )
        assert all(c.status == "supported" for c in claims)  # transitional

    def test_uncited_factual_claim_has_no_indices(self):
        claims = extract_claims("A bare factual claim without any marker.")
        factual = [c for c in claims if c.status == "pending"]
        assert len(factual) == 1
        assert factual[0].citation_indices == []


@pytest.mark.unit
class TestCentralRegeneration:

    def test_majority_uncited_triggers_regenerate(self):
        claims = extract_claims(
            "Uncited fact one. Uncited fact two. Cited fact. [1]"
        )
        reason = find_central_regenerate_reason(claims)
        assert "2/3" in reason

    def test_minority_uncited_does_not(self):
        claims = extract_claims(
            "Cited fact one. [1] Cited fact two. [2] Uncited tail note."
        )
        assert find_central_regenerate_reason(claims) == ""


# ── The entailment output parser ──────────────────────────────────────────────

@pytest.mark.unit
class TestCleanJsonText:

    def test_plain_json(self):
        assert _clean_json_text('{"verdict": "yes"}') == '{"verdict": "yes"}'

    def test_fenced_json(self):
        assert '"no"' in _clean_json_text('```json\n{"verdict": "no"}\n```')

    def test_prose_wrapped_json(self):
        text = 'Sure! {"verdict": "partial"} hope that helps'
        assert '"partial"' in _clean_json_text(text)


# ── Decision logic (mocked entailment) ────────────────────────────────────────

@pytest.mark.unit
class TestValidateAnswer:

    @pytest.mark.asyncio
    async def test_all_cited_and_supported_is_grounded(self):
        extraction = _extraction(
            "The process has four stages. [1] Documents state it. [2]",
            _citation(1, "The process has four stages."),
            _citation(2, "Documents state it."),
        )
        outcome = await validate_answer(extraction, entailment_fn=_yes)
        assert outcome.groundedness == "grounded"
        assert outcome.stripped_claims == 0
        assert outcome.final_text == extraction.cleaned_text
        assert outcome.entailment_checks == 2

    @pytest.mark.asyncio
    async def test_unsupported_claim_stripped_partial(self):
        extraction = _extraction(
            "The process has four stages. [1] Fabricated claim about fees. [2]",
            _citation(1, "The process has four stages."),
            _citation(2, "Something unrelated."),
        )

        async def _no_on_fees(claim, source):
            return ("no", True) if "fees" in claim else ("yes", True)
        outcome = await validate_answer(extraction, entailment_fn=_no_on_fees)
        assert outcome.groundedness == "partial"
        assert outcome.unsupported_claims == 1
        assert outcome.stripped_claims == 1
        assert "fees" not in outcome.final_text
        assert "four stages" in outcome.final_text

    @pytest.mark.asyncio
    async def test_minor_uncited_claim_stripped(self):
        extraction = _extraction(
            "Cited fact is here. [1] A small uncited aside follows.",
            _citation(1, "Cited fact is here."),
        )
        outcome = await validate_answer(extraction, entailment_fn=_yes)
        assert outcome.groundedness == "partial"
        assert outcome.uncited_claims == 1
        assert "uncited aside" not in outcome.final_text
        assert "Cited fact" in outcome.final_text

    @pytest.mark.asyncio
    async def test_central_uncited_signals_regeneration(self):
        extraction = _extraction(
            "Uncited claim one. Uncited claim two. Uncited claim three."
        )
        outcome = await validate_answer(extraction, entailment_fn=_yes)
        assert outcome.should_regenerate is True
        assert "3/3" in outcome.regeneration_reason
        # No stripping yet — the decision precedes the bounded retry.
        assert outcome.final_text == ""

    @pytest.mark.asyncio
    async def test_regenerate_path_disallowed_strips_instead(self):
        extraction = _extraction(
            "Uncited claim one. Uncited claim two."
        )
        outcome = await validate_answer(
            extraction, entailment_fn=_yes, allow_regenerate=False
        )
        assert outcome.should_regenerate is False
        assert outcome.stripped_claims == 2
        assert outcome.emptied is True
        assert outcome.groundedness == "ungrounded"

    @pytest.mark.asyncio
    async def test_entailment_failure_degrades_to_partial_never_silent(self):
        extraction = _extraction(
            "A factual claim that stands. [1]",
            _citation(1, "The supporting source text."),
        )
        outcome = await validate_answer(extraction, entailment_fn=_broken)
        # Kept but NOT counted as verified (roadmap error handling)
        assert outcome.unverified_claims == 1
        assert outcome.entailment_failures == 1
        assert outcome.groundedness == "partial"
        assert "factual claim" in outcome.final_text

    @pytest.mark.asyncio
    async def test_entailment_budget_bounds_checks(self, monkeypatch):
        monkeypatch.setenv("CITATION_ENTAILMENT_MAX_CHECKS", "2")
        from app.core.config import get_settings
        get_settings.cache_clear()
        try:
            extraction = _extraction(
                "Claim one. [1] Claim two. [2] Claim three. [3]",
                _citation(1, "Source one."), _citation(2, "Source two."),
                _citation(3, "Source three."),
            )
            calls = []

            async def _counting(claim, source):
                calls.append(claim)
                return "yes", True
            outcome = await validate_answer(extraction, entailment_fn=_counting)
            assert outcome.entailment_checks == 2       # budget enforced
            assert outcome.unverified_claims == 1       # claim three kept, unverified
            assert outcome.groundedness == "partial"    # never silently passed
            assert "Claim three" in outcome.final_text
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_partial_verdict_keeps_claim(self):
        extraction = _extraction(
            "A related claim. [1]",
            _citation(1, "A related and consistent source."),
        )
        outcome = await validate_answer(extraction, entailment_fn=_partial)
        assert outcome.groundedness == "grounded"
        assert outcome.stripped_claims == 0

    @pytest.mark.asyncio
    async def test_everything_stripped_yields_ungrounded(self):
        extraction = _extraction("Unsupported claim one. Unsupported two.")
        outcome = await validate_answer(
            extraction, entailment_fn=_yes, allow_regenerate=False
        )
        assert outcome.emptied is True
        assert outcome.final_text == ""
        assert outcome.groundedness == "ungrounded"

    @pytest.mark.asyncio
    async def test_validation_disabled_passthrough(self, monkeypatch):
        monkeypatch.setenv("CITATION_VALIDATION_ENABLED", "false")
        from app.core.config import get_settings
        get_settings.cache_clear()
        try:
            extraction = _extraction(
                "Totally uncited claim.",
                _citation(1, "unused"),
            )
            outcome = await validate_answer(extraction, entailment_fn=_no)
            assert outcome.final_text == "Totally uncited claim."
            assert outcome.groundedness == "grounded"
            assert outcome.entailment_checks == 0
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_invalid_markers_never_reach_validation(self):
        # Invalid references are already stripped by resolve_citations —
        # validation only ever sees resolvable citations.
        from app.rag.citations import CitationExtraction
        extraction = CitationExtraction(
            citations=[_citation(1, "Source one.")],
            cleaned_text="A claim. [1]",
            invalid_indices=[3],
        )
        outcome = await validate_answer(extraction, entailment_fn=_yes)
        assert outcome.groundedness == "grounded"
        assert "[3]" not in outcome.final_text
