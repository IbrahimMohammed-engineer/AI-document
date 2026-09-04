"""
Unit tests — comparison narration + semantic-comparison output parsing
(Phase 12, plan §11 Task 9 / §16.1).

Mirrors test_query_analyzer.py's parser-testing style: the defensive JSON
parsing is pure and exercised directly; the LLM calls run against
StubLLMProvider (happy path) and a raising stub (failure path).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.llm import LLMProviderError, StubLLMProvider
from app.rag.comparison_narration import (
    _clip_to_budget,
    _fallback_narration,
    classify_section_semantically,
    narrate_changes,
    parse_semantic_comparison,
)


def _change(
    *,
    change_type: str = "MODIFIED",
    severity: str = "MAJOR",
    section: str = "3.1 Approval Process",
    old_text: str | None = "Approval within 5 business days.",
    new_text: str | None = "Approval within 7 business days.",
) -> SimpleNamespace:
    return SimpleNamespace(
        change_type=change_type,
        severity=severity,
        section=section,
        old_text=old_text,
        new_text=new_text,
    )


# ── parse_semantic_comparison (pure) ──────────────────────────────────────────

@pytest.mark.unit
class TestParseSemanticComparison:

    def test_happy_path_material(self):
        raw = '{"materiality": "material", "rationale": "The deadline changed."}'
        materiality, rationale = parse_semantic_comparison(raw)
        assert materiality == "material"
        assert rationale == "The deadline changed."

    def test_happy_path_stylistic(self):
        materiality, _ = parse_semantic_comparison(
            '{"materiality": "stylistic", "rationale": "must → shall."}'
        )
        assert materiality == "stylistic"

    def test_markdown_fences_stripped(self):
        raw = '```json\n{"materiality": "material", "rationale": "ok"}\n```'
        materiality, _ = parse_semantic_comparison(raw)
        assert materiality == "material"

    def test_prose_around_json_stripped(self):
        materiality, _ = parse_semantic_comparison(
            'Answer: {"materiality": "stylistic"} — done.'
        )
        assert materiality == "stylistic"

    def test_missing_materiality_returns_none(self):
        assert parse_semantic_comparison('{"rationale": "no field"}') == (None, None)

    def test_invalid_materiality_value_returns_none(self):
        assert parse_semantic_comparison(
            '{"materiality": "catastrophic", "rationale": "bad"}'
        ) == (None, None)

    def test_malformed_json_returns_none(self):
        assert parse_semantic_comparison("not json at all") == (None, None)

    def test_non_object_json_returns_none(self):
        assert parse_semantic_comparison('["materiality"]') == (None, None)

    def test_empty_string_returns_none(self):
        assert parse_semantic_comparison("") == (None, None)

    def test_missing_rationale_still_returns_materiality(self):
        materiality, rationale = parse_semantic_comparison('{"materiality": "material"}')
        assert materiality == "material"
        assert rationale is None

    def test_rationale_truncated_to_200_chars(self):
        raw = '{"materiality": "material", "rationale": "' + "x" * 500 + '"}'
        _, rationale = parse_semantic_comparison(raw)
        assert rationale is not None and len(rationale) == 200


# ── classify_section_semantically (LLM boundary) ──────────────────────────────

@pytest.mark.unit
class TestClassifySectionSemantically:

    @pytest.mark.asyncio
    async def test_happy_path(self):
        provider = StubLLMProvider(
            responder=lambda messages: '{"materiality": "material", "rationale": "days changed"}'
        )
        materiality, truncated = await classify_section_semantically(
            "5 business days", "7 business days", provider=provider
        )
        assert materiality == "material"
        assert truncated is False

    @pytest.mark.asyncio
    async def test_provider_error_returns_none_not_raise(self):
        """§9.6 failure handling: an LLM outage degrades to materiality=None —
        the pipeline must continue (§9.8 None branch)."""

        class _RaisingProvider(StubLLMProvider):
            async def generate(self, *args, **kwargs):  # type: ignore[override]
                raise LLMProviderError("provider down", code="LLM_ERROR")

        materiality, truncated = await classify_section_semantically(
            "old", "new", provider=_RaisingProvider()
        )
        assert materiality is None
        assert truncated is False

    @pytest.mark.asyncio
    async def test_unparseable_output_returns_none(self):
        provider = StubLLMProvider(responder=lambda messages: "I think it changed a lot!")
        materiality, _ = await classify_section_semantically(
            "old", "new", provider=provider
        )
        assert materiality is None

    def test_clip_to_budget_marks_truncation(self):
        old_clipped, new_clipped, truncated = _clip_to_budget(
            "x" * 20_000, "short", token_counter=None
        )
        assert truncated is True
        assert old_clipped.endswith("[...]")
        assert new_clipped == "short"

    def test_clip_to_budget_no_truncation(self):
        _, _, truncated = _clip_to_budget("short old", "short new", token_counter=None)
        assert truncated is False


# ── narrate_changes (§14) ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestNarrateChanges:

    @pytest.mark.asyncio
    async def test_narration_uses_llm_output(self):
        changes = [_change()]
        provider = StubLLMProvider(
            responder=lambda messages: "The approval window changed from 5 to 7 days (MAJOR)."
        )
        narration = await narrate_changes(changes, provider=provider)  # type: ignore[arg-type]
        assert "5 to 7 days" in narration

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_structured_summary(self):
        class _RaisingProvider(StubLLMProvider):
            async def generate(self, *args, **kwargs):  # type: ignore[override]
                raise LLMProviderError("down", code="LLM_ERROR")

        changes = [_change(), _change(change_type="ADDED", severity="MODERATE",
                                      section="5.2 New Appendix", old_text=None)]
        narration = await narrate_changes(changes, provider=_RaisingProvider())  # type: ignore[arg-type]
        assert "MAJOR" in narration
        assert "MODERATE" in narration
        assert "Approval Process" in narration

    @pytest.mark.asyncio
    async def test_empty_changes_short_circuits(self):
        narration = await narrate_changes([], provider=StubLLMProvider())
        assert "No changes" in narration

    def test_fallback_narration_groups_by_severity(self):
        changes = [
            _change(severity="MINOR", section="1.1 Title"),
            _change(severity="MAJOR", section="3.1 Approval"),
            _change(severity="MODERATE", section="4.0 Scope", change_type="REMOVED"),
        ]
        text = _fallback_narration(changes)  # type: ignore[arg-type]
        lines = text.splitlines()
        # MAJOR group appears before MODERATE before MINOR
        major_idx = next(i for i, l in enumerate(lines) if "MAJOR changes" in l)
        moderate_idx = next(i for i, l in enumerate(lines) if "MODERATE changes" in l)
        minor_idx = next(i for i, l in enumerate(lines) if "MINOR changes" in l)
        assert major_idx < moderate_idx < minor_idx
