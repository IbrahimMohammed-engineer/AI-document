"""
Unit tests — contradiction-check output parsing (Phase 13, plan §23 Task 6).

Mirrors test_query_analyzer.py's parser-testing style: valid output,
is_conflict=false, malformed JSON, out-of-range confidence clamping,
missing fields, markdown fences, and non-bool is_conflict.
"""
from __future__ import annotations

import json

import pytest

from app.rag.conflict_parsing import ContradictionResult, parse_contradiction_check

pytestmark = pytest.mark.unit


def _payload(**overrides) -> str:
    base = {
        "is_conflict": True,
        "confidence": 0.9,
        "reason": "Different approvers for the same requirement.",
        "conflict_topic": "Vacation Approval Authority",
    }
    base.update(overrides)
    return json.dumps(base)


class TestParseContradictionCheck:

    def test_valid_conflict(self):
        result = parse_contradiction_check(_payload())
        assert result is not None
        assert result.is_conflict is True
        assert result.confidence == 0.9
        assert "approvers" in (result.reason or "")
        assert result.conflict_topic == "Vacation Approval Authority"

    def test_valid_non_conflict(self):
        result = parse_contradiction_check(_payload(is_conflict=False, confidence=0.4))
        assert result is not None
        assert result.is_conflict is False
        assert result.confidence == 0.4

    def test_malformed_json_returns_none(self):
        assert parse_contradiction_check("not json at all") is None
        assert parse_contradiction_check("") is None
        assert parse_contradiction_check("[1, 2, 3]") is None

    def test_markdown_fences_stripped(self):
        raw = "```json\n" + _payload() + "\n```"
        result = parse_contradiction_check(raw)
        assert result is not None
        assert result.is_conflict is True

    def test_prose_around_json_locates_object(self):
        raw = "Here is my answer:\n" + _payload() + "\nHope that helps."
        result = parse_contradiction_check(raw)
        assert result is not None
        assert result.is_conflict is True

    def test_confidence_above_one_clamped_not_rejected(self):
        result = parse_contradiction_check(_payload(confidence=1.7))
        assert result is not None
        assert result.confidence == 1.0

    def test_confidence_below_zero_clamped_not_rejected(self):
        result = parse_contradiction_check(_payload(confidence=-2.0))
        assert result is not None
        assert result.confidence == 0.0

    def test_non_bool_is_conflict_rejected(self):
        assert parse_contradiction_check(_payload(is_conflict="true")) is None
        assert parse_contradiction_check(_payload(is_conflict=1)) is None
        assert parse_contradiction_check(_payload(is_conflict=None)) is None

    def test_non_numeric_confidence_rejected(self):
        assert parse_contradiction_check(_payload(confidence="high")) is None
        assert parse_contradiction_check(_payload(confidence=None)) is None

    def test_missing_optional_fields_default(self):
        raw = json.dumps({"is_conflict": True, "confidence": 0.7})
        result = parse_contradiction_check(raw)
        assert result is not None
        assert result.reason is None
        assert result.conflict_topic is None

    def test_topic_truncated_to_bounded_length(self):
        from app.domain.conflict_rules import CONFLICT_TOPIC_MAX_CHARS

        long_topic = "X" * (CONFLICT_TOPIC_MAX_CHARS + 50)
        result = parse_contradiction_check(_payload(conflict_topic=long_topic))
        assert result is not None
        assert len(result.conflict_topic) == CONFLICT_TOPIC_MAX_CHARS

    def test_reason_truncated(self):
        result = parse_contradiction_check(_payload(reason="y" * 1_000))
        assert result is not None
        assert len(result.reason) == 300

    def test_result_type_shape(self):
        result = parse_contradiction_check(_payload())
        assert isinstance(result, ContradictionResult)
