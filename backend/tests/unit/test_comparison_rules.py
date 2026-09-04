"""
Unit tests for app/domain/comparison_rules.py (Phase 12).

Tests all branches of classify_severity() and compute_proportion_changed().
Pure functions — no DB, no I/O.
"""
from __future__ import annotations

import pytest

from app.domain.comparison_rules import (
    classify_severity,
    compute_proportion_changed,
    is_critical_section,
)


class TestComputeProportionChanged:
    def test_identical_returns_zero(self):
        assert compute_proportion_changed("hello world", "hello world") == 0.0

    def test_completely_different_approaches_one(self):
        ratio = compute_proportion_changed("foo bar baz", "alpha beta gamma")
        assert ratio >= 0.5

    def test_partial_change(self):
        ratio = compute_proportion_changed("a b c d e", "a b c x y")
        assert 0.0 < ratio < 1.0

    def test_both_empty_returns_one(self):
        # Degenerate: empty→empty means "fully different" by convention
        assert compute_proportion_changed("", "") == 1.0

    def test_one_empty(self):
        ratio = compute_proportion_changed("", "hello world foo")
        assert ratio == 1.0


class TestClassifySeverity:
    """Tests the first-match decision tree documented in the module."""

    # Rule 1: critical section → always MAJOR
    def test_critical_section_always_major(self):
        for change_type in ("ADDED", "REMOVED", "MODIFIED"):
            result = classify_severity(
                change_type=change_type,
                semantic_materiality=None,
                is_critical_section=True,
                proportion_changed=0.0,
            )
            assert result == "MAJOR", f"Expected MAJOR for {change_type} + critical"

    # Rule 2: ADDED/REMOVED non-critical → MODERATE
    def test_added_non_critical_moderate(self):
        result = classify_severity(
            change_type="ADDED",
            semantic_materiality=None,
            is_critical_section=False,
            proportion_changed=0.9,
        )
        assert result == "MODERATE"

    def test_removed_non_critical_moderate(self):
        result = classify_severity(
            change_type="REMOVED",
            semantic_materiality=None,
            is_critical_section=False,
            proportion_changed=0.9,
        )
        assert result == "MODERATE"

    # Rule 3: MODIFIED + material + proportion >= 0.30 → MAJOR
    def test_modified_material_high_proportion_major(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality="material",
            is_critical_section=False,
            proportion_changed=0.50,
        )
        assert result == "MAJOR"

    # Rule 4: MODIFIED + material + proportion < 0.30 → MODERATE
    def test_modified_material_low_proportion_moderate(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality="material",
            is_critical_section=False,
            proportion_changed=0.15,
        )
        assert result == "MODERATE"

    # Rule 5: MODIFIED + stylistic → MINOR
    def test_modified_stylistic_minor(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality="stylistic",
            is_critical_section=False,
            proportion_changed=0.80,
        )
        assert result == "MINOR"

    # Rule 6: MODIFIED + no materiality + proportion >= 0.50 → MODERATE
    def test_modified_no_materiality_high_moderate(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality=None,
            is_critical_section=False,
            proportion_changed=0.60,
        )
        assert result == "MODERATE"

    # Rule 7: MODIFIED + no materiality + proportion < 0.50 → MINOR
    def test_modified_no_materiality_low_minor(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality=None,
            is_critical_section=False,
            proportion_changed=0.30,
        )
        assert result == "MINOR"

    # Threshold boundary tests
    def test_material_threshold_exact_30_is_major(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality="material",
            is_critical_section=False,
            proportion_changed=0.30,
        )
        assert result == "MAJOR"

    def test_no_materiality_threshold_exact_50_is_moderate(self):
        result = classify_severity(
            change_type="MODIFIED",
            semantic_materiality=None,
            is_critical_section=False,
            proportion_changed=0.50,
        )
        assert result == "MODERATE"


class TestIsCriticalSection:
    def test_empty_patterns_never_critical(self):
        assert is_critical_section("Penalties", "5.3", []) is False

    def test_matching_title_substring(self):
        assert is_critical_section("Penalty Clause", None, ["penalty"]) is True

    def test_matching_section_number_substring(self):
        assert is_critical_section(None, "5.1.3", ["5.1"]) is True

    def test_case_insensitive(self):
        assert is_critical_section("DEFINITIONS", None, ["definitions"]) is True

    def test_no_match(self):
        assert is_critical_section("Introduction", "1.0", ["termination"]) is False

    def test_none_section_fields(self):
        assert is_critical_section(None, None, ["anything"]) is False
