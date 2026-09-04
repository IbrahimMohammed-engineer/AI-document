"""
Conflict-detection domain rules — pure, unit-testable functions and constants.

Phase 13.  This module owns every tunable threshold and the deterministic
severity classifier for detected conflicts.  Following the Phase 12
convention (``domain/comparison_rules.py``), all numbers live here as named
constants so they are trivially discoverable and tunable — none are inline
magic numbers in service code.

All thresholds are plan-authored defaults (PHASE-13-IMPLEMENTATION-PLAN.md
§11, §23 Task 1), chosen conservatively per the roadmap's "start
conservative — fewer, high-confidence conflicts" guidance.

Severity rule set (``classify_conflict_severity``):

  is_critical_section → always MAJOR
  statement_count >= 3:  confidence >= 0.75 → MAJOR, else MODERATE
  statement_count == 2:  confidence >= 0.85 → MODERATE, else MINOR
"""
from __future__ import annotations

from typing import Literal

from app.domain.comparison_rules import is_critical_section  # noqa: F401  (re-export)

# ── Tunable constants (plan-authored defaults — see module docstring) ─────────

# Candidate generation: nearest cross-document neighbours considered per chunk.
CANDIDATE_TOP_K = 5

# Candidate generation: minimum cosine similarity for a chunk pair to be
# promoted to a contradiction-check candidate (0.0–1.0).
CANDIDATE_SIMILARITY_THRESHOLD = 0.83

# A candidate is persisted as a conflict only when the contradiction check
# reports is_conflict=true AND confidence >= this value.  Below threshold —
# or on any provider/parse failure — the candidate is skipped, never guessed.
CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD = 0.6

# Per-statement token budget for the contradiction-check LLM call (a safety
# cap for pathological outlier chunks — chunks are already bounded retrieval
# units well under this).
CONTRADICTION_CHECK_TOKEN_BUDGET = 1_000

# Length cap for the LLM-provided conflict_topic label (mirrors the analyzer's
# topic truncation convention).
CONFLICT_TOPIC_MAX_CHARS = 80

Severity = Literal["MAJOR", "MODERATE", "MINOR"]


def classify_conflict_severity(
    *,
    is_critical_section: bool,
    confidence: float,
    statement_count: int,
) -> Severity:
    """Deterministically classify a confirmed conflict's severity.

    Args:
        is_critical_section: True when any statement's section matches the
            org's ``settings["comparison"]["critical_sections"]`` patterns
            (the SAME lookup the comparison severity rule uses — shared via
            the re-exported :func:`is_critical_section`).
        confidence:      The contradiction check's confidence in [0, 1].
        statement_count: Number of statements in the conflict (>= 2).

    Decision tree (first matching branch wins):
      1. is_critical_section → MAJOR
      2. statement_count >= 3 → MAJOR if confidence >= 0.75 else MODERATE
      3. otherwise            → MODERATE if confidence >= 0.85 else MINOR
    """
    if is_critical_section:
        return "MAJOR"
    if statement_count >= 3:
        return "MAJOR" if confidence >= 0.75 else "MODERATE"
    if confidence >= 0.85:
        return "MODERATE"
    return "MINOR"
