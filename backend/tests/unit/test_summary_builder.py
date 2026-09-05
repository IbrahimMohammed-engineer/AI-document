"""
Unit tests — summary builder parsing (Phase 14, plan §5.8/§8.1).

Covers: parse success; malformed-JSON retry-then-raise; the defensive
string-list sanitization.
"""
from __future__ import annotations

import json

import pytest

from app.rag.summary_builder import (
    SummaryDraft,
    SummaryParseError,
    parse_summary_draft,
)

VALID = json.dumps({
    "executive_summary": "The policy covers leave. [1]",
    "key_points": ["Twenty days annual leave. [1]", "One week notice. [2]"],
    "dates": ["Effective 2026-01-01. [3]"],
    "roles": ["Managers approve. [2]"],
    "requirements": ["Notice required. [2]"],
    "risks": ["Termination for violations. [3]"],
    "topics": ["leave", "discipline"],
})


@pytest.mark.unit
class TestParseSummaryDraft:

    def test_valid_output_parses(self):
        draft = parse_summary_draft(VALID)
        assert isinstance(draft, SummaryDraft)
        assert draft.executive_summary == "The policy covers leave. [1]"
        assert len(draft.key_points) == 2
        assert draft.topics == ["leave", "discipline"]
        # Every citable item carries a citation marker.
        for item in draft.key_points + draft.dates + draft.roles:
            assert "[1]" in item or "[2]" in item or "[3]" in item

    def test_markdown_fences_stripped(self):
        draft = parse_summary_draft(f"```json\n{VALID}\n```")
        assert draft.executive_summary.startswith("The policy covers leave.")

    def test_prose_around_json_stripped(self):
        draft = parse_summary_draft(f"Here it is:\n{VALID}\nDone.")
        assert len(draft.key_points) == 2

    def test_invalid_json_raises(self):
        with pytest.raises(SummaryParseError):
            parse_summary_draft("not json at all")

    def test_non_object_raises(self):
        with pytest.raises(SummaryParseError):
            parse_summary_draft('["executive_summary"]')

    def test_missing_fields_default_to_empty(self):
        draft = parse_summary_draft("{}")
        assert draft.executive_summary == ""
        assert draft.key_points == []
        assert draft.topics == []

    def test_non_string_items_dropped(self):
        raw = json.dumps({
            "key_points": ["valid [1]", 42, ["nested"], "", "  "],
        })
        draft = parse_summary_draft(raw)
        assert draft.key_points == ["valid [1]"]
