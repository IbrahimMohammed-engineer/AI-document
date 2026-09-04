"""
Unit tests for app/domain/text_diff.py (Phase 12).

Tests normalization and diff computation.
Pure functions — no DB, no I/O.
"""
from __future__ import annotations

import pytest

from app.domain.text_diff import (
    TextDiffResult,
    diff_text,
    normalize_text,
    proportion_from_diff,
)


class TestNormalizeText:
    def test_collapses_internal_whitespace(self):
        assert normalize_text("hello   world") == "hello world"

    def test_normalizes_windows_line_endings(self):
        assert normalize_text("line1\r\nline2") == "line1\nline2"

    def test_strips_leading_trailing(self):
        assert normalize_text("  hello  ") == "hello"

    def test_collapses_consecutive_blank_lines(self):
        result = normalize_text("a\n\n\nb")
        assert "\n\n\n" not in result

    def test_preserves_single_blank_line(self):
        result = normalize_text("a\n\nb")
        assert result == "a\n\nb"


class TestDiffText:
    def test_identical_texts_unchanged(self):
        result = diff_text("the quick brown fox", "the quick brown fox")
        assert result.is_unchanged is True
        assert result.changed_token_count == 0

    def test_whitespace_only_diff_unchanged(self):
        result = diff_text("hello   world", "hello world")
        assert result.is_unchanged is True

    def test_content_change_detected(self):
        result = diff_text("The penalty is 10%.", "The penalty is 20%.")
        assert result.is_unchanged is False
        assert result.changed_token_count > 0

    def test_complete_replacement_high_proportion(self):
        result = diff_text("foo bar baz qux", "alpha beta gamma delta")
        assert result.is_unchanged is False
        prop = proportion_from_diff(result)
        assert prop > 0.5

    def test_empty_both(self):
        # Both empty → normalization makes them both "" → equal
        result = diff_text("", "")
        assert result.is_unchanged is True

    def test_diff_spans_present_on_change(self):
        result = diff_text("The fee is $10 per item.", "The fee is $20 per item.")
        assert result.is_unchanged is False
        assert len(result.diff_spans) > 0

    def test_proportion_zero_for_unchanged(self):
        result = diff_text("same text", "same text")
        assert proportion_from_diff(result) == 0.0

    def test_proportion_bounded_0_to_1(self):
        result = diff_text("a b c d e f g h i j", "z y x w v u t s r q")
        assert result.is_unchanged is False
        prop = proportion_from_diff(result)
        assert 0.0 <= prop <= 1.0
