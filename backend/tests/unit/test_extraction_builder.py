"""
Unit tests — extraction builder parsing (Phase 14, plan §5.10/§8.1).

Covers: parse success; malformed-JSON retry-then-raise; oversized-category
truncation (extraction_max_items_per_category).
"""
from __future__ import annotations

import json

import pytest

from app.rag.extraction_builder import (
    ExtractionDraft,
    ExtractionParseError,
    parse_extraction_draft,
)


def _draft(items_per_category: int) -> str:
    return json.dumps({
        "requirement": [f"req {i} [1]" for i in range(items_per_category)],
        "risk": [f"risk {i} [2]" for i in range(items_per_category)],
        "date": [f"date {i} [3]" for i in range(items_per_category)],
        "party": [f"party {i} [4]" for i in range(items_per_category)],
    })


@pytest.mark.unit
class TestParseExtractionDraft:

    def test_valid_output_parses(self):
        draft = parse_extraction_draft(_draft(2), max_items_per_category=20)
        assert isinstance(draft, ExtractionDraft)
        assert draft.requirement == ["req 0 [1]", "req 1 [1]"]
        assert draft.party == ["party 0 [4]", "party 1 [4]"]
        assert draft.items_for("date") == ["date 0 [3]", "date 1 [3]"]

    def test_markdown_fences_stripped(self):
        draft = parse_extraction_draft(
            f"```json\n{_draft(1)}\n```", max_items_per_category=20
        )
        assert len(draft.risk) == 1

    def test_invalid_json_raises(self):
        with pytest.raises(ExtractionParseError):
            parse_extraction_draft("nope", max_items_per_category=20)

    def test_non_object_raises(self):
        with pytest.raises(ExtractionParseError):
            parse_extraction_draft("[]", max_items_per_category=20)

    def test_missing_categories_default_empty(self):
        draft = parse_extraction_draft("{}", max_items_per_category=20)
        assert draft.requirement == []
        assert draft.party == []

    def test_oversized_category_truncated(self):
        # The defensive cap (plan §5.3/§5.10) truncates any category beyond
        # max_items_per_category — untrusted output must not balloon.
        draft = parse_extraction_draft(_draft(50), max_items_per_category=20)
        assert len(draft.requirement) == 20
        assert len(draft.risk) == 20
        assert len(draft.date) == 20
        assert len(draft.party) == 20

    def test_non_string_items_dropped(self):
        raw = json.dumps({"requirement": ["ok [1]", 7, None, ""]})
        draft = parse_extraction_draft(raw, max_items_per_category=20)
        assert draft.requirement == ["ok [1]"]
