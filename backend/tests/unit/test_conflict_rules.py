"""
Unit tests — conflict severity rules + thresholds (Phase 13, plan §23 Task 1).

Covers every branch of classify_conflict_severity, the exported threshold
constants, and the is_critical_section re-export (the SHARED Phase 12
lookup — extraction parity with test_comparison_rules.py).
"""
from __future__ import annotations

import pytest

from app.domain import conflict_rules
from app.domain.conflict_rules import (
    CANDIDATE_SIMILARITY_THRESHOLD,
    CANDIDATE_TOP_K,
    CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD,
    CONTRADICTION_CHECK_TOKEN_BUDGET,
    classify_conflict_severity,
    is_critical_section,
)

pytestmark = pytest.mark.unit


# ── classify_conflict_severity — every branch (§23 Task 1) ────────────────────

class TestClassifyConflictSeverity:

    def test_critical_section_always_major(self):
        # Critical wins regardless of confidence or statement count.
        assert classify_conflict_severity(
            is_critical_section=True, confidence=0.0, statement_count=2
        ) == "MAJOR"
        assert classify_conflict_severity(
            is_critical_section=True, confidence=0.6, statement_count=5
        ) == "MAJOR"

    @pytest.mark.parametrize("count", [3, 4, 10])
    def test_three_or_more_statements_confidence_split(self, count: int):
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.75, statement_count=count
        ) == "MAJOR"
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.74, statement_count=count
        ) == "MODERATE"
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.99, statement_count=count
        ) == "MAJOR"

    def test_two_statements_confidence_split(self):
        # confidence >= 0.85 → MODERATE (never MAJOR on 2 statements alone)
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.85, statement_count=2
        ) == "MODERATE"
        assert classify_conflict_severity(
            is_critical_section=False, confidence=1.0, statement_count=2
        ) == "MODERATE"
        # confidence < 0.85 → MINOR
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.84, statement_count=2
        ) == "MINOR"
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.0, statement_count=2
        ) == "MINOR"

    def test_boundaries_are_inclusive(self):
        # 0.75 (3 statements) → MAJOR; 0.85 (2 statements) → MODERATE
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.75, statement_count=3
        ) == "MAJOR"
        assert classify_conflict_severity(
            is_critical_section=False, confidence=0.85, statement_count=2
        ) == "MODERATE"


# ── Threshold constants (plan-authored defaults, discoverable + tunable) ──────

class TestThresholdConstants:

    def test_values(self):
        assert CANDIDATE_TOP_K == 5
        assert CANDIDATE_SIMILARITY_THRESHOLD == 0.83
        assert CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD == 0.6
        assert CONTRADICTION_CHECK_TOKEN_BUDGET == 1_000

    def test_confidence_threshold_below_severity_breakpoints(self):
        # A confirmed conflict (0.6) starts at MINOR severity for 2
        # statements — severity escalates with evidence/confidence only.
        assert CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD < 0.85


# ── Shared critical-section lookup (Phase 12 parity) ──────────────────────────

class TestIsCriticalSectionReexport:

    def test_reexport_is_the_phase12_function(self):
        from app.domain.comparison_rules import (
            is_critical_section as original,
        )
        assert is_critical_section is original

    def test_matching_pattern_is_critical(self):
        assert is_critical_section("Approval Process", "3.1", ["approval"]) is True

    def test_empty_patterns_never_critical(self):
        assert is_critical_section("Approval Process", "3.1", []) is False

    def test_case_insensitive(self):
        assert conflict_rules.is_critical_section(
            "TERMINATION CLAUSE", None, ["termination clause"]
        ) is True
