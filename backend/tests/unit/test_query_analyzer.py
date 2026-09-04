"""
Unit tests — query analyzer (Phase 9, Backend §27).

Covers: constrained-JSON parsing (roadmap Phase 9 §Testing "analyzer output
parsing"), malformed-output retry-then-default, provider-failure graceful
degradation, and the deferred-intent classification.
"""
from __future__ import annotations

import pytest

from app.infrastructure.llm import (
    LLMMessage,
    LLMProviderError,
    StubLLMProvider,
)
from app.rag.query_analyzer import (
    AnalyzerParseError,
    analyze_query,
    default_analysis,
    parse_analyzer_output,
)


VALID_JSON = (
    '{"intent": "QUESTION", "temporal_scope": {"year": 2025}, '
    '"scope_hints": ["marketing policy"], "topic": "approval_process"}'
)


# ── Parsing (pure) ────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestParseAnalyzerOutput:

    def test_valid_full_output(self):
        analysis = parse_analyzer_output(VALID_JSON)
        assert analysis.intent == "QUESTION"
        assert analysis.temporal_scope == {"year": 2025}
        assert analysis.scope_hints == ["marketing policy"]
        assert analysis.topic == "approval_process"

    def test_markdown_fences_stripped(self):
        raw = f"```json\n{VALID_JSON}\n```"
        analysis = parse_analyzer_output(raw)
        assert analysis.intent == "QUESTION"

    def test_prose_around_json_stripped(self):
        analysis = parse_analyzer_output(f"Here you go:\n{VALID_JSON}\nThanks!")
        assert analysis.topic == "approval_process"

    def test_intent_case_insensitive(self):
        analysis = parse_analyzer_output('{"intent": "comparison"}')
        assert analysis.intent == "COMPARISON"
        # Phase 12: COMPARISON is now a LIVE intent (routed to the comparison
        # service) — only SUMMARY/CONFLICT_DETECTION/EXTRACTION stay deferred.
        assert not analysis.is_deferred_intent

    def test_deferred_intents_classified(self):
        for intent in ("CHANGE_DETECTION", "SUMMARY", "CONFLICT_DETECTION", "EXTRACTION"):
            assert parse_analyzer_output(f'{{"intent": "{intent}"}}').intent == intent

    def test_only_phase14_intents_remain_deferred(self):
        assert parse_analyzer_output('{"intent": "summary"}').is_deferred_intent
        assert parse_analyzer_output('{"intent": "conflict_detection"}').is_deferred_intent
        assert parse_analyzer_output('{"intent": "extraction"}').is_deferred_intent
        assert not parse_analyzer_output('{"intent": "change_detection"}').is_deferred_intent

    def test_invalid_json_raises(self):
        with pytest.raises(AnalyzerParseError):
            parse_analyzer_output("not json at all")

    def test_non_object_raises(self):
        with pytest.raises(AnalyzerParseError):
            parse_analyzer_output('["intent"]')

    def test_unknown_intent_raises(self):
        with pytest.raises(AnalyzerParseError):
            parse_analyzer_output('{"intent": "TRANSLATE"}')

    def test_temporal_scope_normalization(self):
        assert parse_analyzer_output(
            '{"temporal_scope": {"year": "2024"}}'
        ).temporal_scope == {"year": 2024}
        assert parse_analyzer_output(
            '{"temporal_scope": {"relative": "Current"}}'
        ).temporal_scope == {"relative": "current"}
        assert parse_analyzer_output(
            '{"temporal_scope": "nonsense"}'
        ).temporal_scope is None

    def test_scope_hints_sanitized_and_bounded(self):
        long_hint = "a" * 200
        analysis = parse_analyzer_output(
            '{"scope_hints": ["  HR Policy ", "", 42, ["nested"], "'
            + long_hint
            + '", "b", "c", "d", "e", "f", "g", "h", "i"]}'
        )
        assert all(isinstance(h, str) and h.strip() == h for h in analysis.scope_hints)
        assert len(analysis.scope_hints) <= 10

    def test_missing_fields_default(self):
        analysis = parse_analyzer_output("{}")
        assert analysis.intent == "QUESTION"
        assert analysis.temporal_scope is None
        assert analysis.scope_hints == []
        assert analysis.topic is None


# ── Stage behaviour ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestAnalyzeQuery:

    async def test_success_sets_flags(self):
        provider = StubLLMProvider(responder=lambda msgs: VALID_JSON)
        analysis = await analyze_query("What is the approval process?", provider=provider)
        assert analysis.analyzer_used_llm is True
        assert analysis.topic == "approval_process"
        assert analysis.model == "stub-llm"

    async def test_malformed_twice_degrades_to_default(self):
        provider = StubLLMProvider(responder=lambda msgs: "I am not JSON")
        analysis = await analyze_query("question", provider=provider)
        assert analysis == default_analysis() or (
            analysis.intent == "QUESTION" and analysis.analyzer_used_llm is False
        )
        # one constrained-schema retry (2 LLM calls total)
        assert provider.call_count == 2

    async def test_malformed_then_valid_recovers(self):
        responses = iter(["not json", '{"intent": "SUMMARY", "topic": "policy_summary"}'])

        def responder(msgs):
            return next(responses)

        analysis = await analyze_query("summarize this", provider=StubLLMProvider(responder=responder))
        assert analysis.intent == "SUMMARY"
        assert analysis.analyzer_used_llm is True

    async def test_provider_failure_degrades_gracefully(self):
        class FailingProvider(StubLLMProvider):
            async def generate(self, messages, **kwargs):
                raise LLMProviderError("down", code="LLM_SERVER_ERROR")

        analysis = await analyze_query("question", provider=FailingProvider())
        assert analysis.intent == "QUESTION"
        assert analysis.analyzer_used_llm is False

    async def test_no_provider_degrades(self):
        analysis = await analyze_query("question", provider=None)
        # Note: the process global is None in unit-test context
        assert analysis.intent == "QUESTION"

    async def test_retry_includes_corrective_message(self):
        seen: list[list[LLMMessage]] = []

        def responder(msgs):
            seen.append(list(msgs))
            return "nope"

        await analyze_query("q", provider=StubLLMProvider(responder=responder))
        assert len(seen) == 2
        # The retry carries the assistant's bad output + a corrective turn
        roles = [m.role for m in seen[1]]
        assert roles == ["system", "user", "assistant", "user"]
