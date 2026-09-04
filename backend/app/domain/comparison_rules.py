"""
Comparison severity classification and change proportion — pure domain functions.

These functions embody the business rules for classifying how significant a
detected change is (MAJOR / MODERATE / MINOR).  They are pure (no I/O, no ORM
imports) so they can be unit-tested exhaustively without a database.

Severity rule set (§9.8 — plan-authored concrete thresholds, not verbatim
from architecture docs, but consistent with the three qualitative inputs
described in Backend §40):

  is_critical_section → always MAJOR
  ADDED / REMOVED (non-critical) → MODERATE
  MODIFIED + materiality = "material" + proportion >= 0.3 → MAJOR
  MODIFIED + materiality = "material" + proportion < 0.3  → MODERATE
  MODIFIED + materiality = "stylistic"                    → MINOR
  MODIFIED + materiality = None (semantic call failed):
      proportion >= 0.5 → MODERATE
      proportion < 0.5  → MINOR

``is_critical_section`` is determined by the caller by comparing the section
title / number against ``organization.settings["comparison"]["critical_sections"]``
(a list of case-insensitive substrings — empty list by default, meaning no
section is automatically critical until an org configures one).

See PHASE-12-IMPLEMENTATION-PLAN.md §9.8.
"""
from __future__ import annotations

import difflib
import re
from typing import Literal


# ── Proportion changed ────────────────────────────────────────────────────────

def compute_proportion_changed(old_text: str, new_text: str) -> float:
    """Word-level changed-token ratio (0.0 = identical, 1.0 = completely different).

    Uses ``difflib.SequenceMatcher`` at the word-token level, which is the same
    instance already used by ``text_diff.diff_text`` — callers that already have
    the matcher result may pass ``proportion_changed`` directly rather than
    calling this function again.

    Formula:
        changed_tokens / max(len(old_words), len(new_words))
    where ``changed_tokens`` = total insertions + deletions from the diff.

    Returns 1.0 when both inputs are empty (degenerate; should not arise in
    practice because empty→empty sections are UNCHANGED by the hash check).
    """
    old_words = _word_tokens(old_text)
    new_words = _word_tokens(new_text)

    total = max(len(old_words), len(new_words))
    if total == 0:
        return 1.0

    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    # Count matching blocks to derive the changed count
    matching = sum(block.size for block in matcher.get_matching_blocks())
    changed = total - matching
    return min(1.0, max(0.0, changed / total))


def _word_tokens(text: str) -> list[str]:
    """Split text into lower-cased word tokens for diff comparison."""
    return re.findall(r"\w+", text.lower())


# ── Severity classification ───────────────────────────────────────────────────

def classify_severity(
    *,
    change_type: Literal["ADDED", "REMOVED", "MODIFIED"],
    semantic_materiality: Literal["material", "stylistic"] | None,
    is_critical_section: bool,
    proportion_changed: float,
) -> Literal["MAJOR", "MODERATE", "MINOR"]:
    """Classify the severity of a detected change.

    Args:
        change_type:          ADDED / REMOVED / MODIFIED (deterministic, from §9.4/§9.5).
        semantic_materiality: "material" or "stylistic" from the LLM call (§9.6),
                              or None if the semantic stage was skipped or failed.
        is_critical_section:  True when the section title/number matches a
                              configured critical-section pattern
                              (``org.settings["comparison"]["critical_sections"]``).
        proportion_changed:   Word-token changed ratio 0.0–1.0 (from §9.5).

    Returns:
        "MAJOR", "MODERATE", or "MINOR".

    Decision tree (first matching branch wins):
      1. is_critical_section → MAJOR  (regardless of type/materiality)
      2. ADDED / REMOVED (non-critical) → MODERATE
      3. MODIFIED + material + proportion >= 0.30 → MAJOR
      4. MODIFIED + material + proportion  < 0.30 → MODERATE
      5. MODIFIED + stylistic → MINOR
      6. MODIFIED + no materiality + proportion >= 0.50 → MODERATE
      7. MODIFIED + no materiality + proportion  < 0.50 → MINOR
    """
    if is_critical_section:
        return "MAJOR"

    if change_type in ("ADDED", "REMOVED"):
        return "MODERATE"

    # change_type == "MODIFIED" from here
    if semantic_materiality == "material":
        return "MAJOR" if proportion_changed >= 0.30 else "MODERATE"

    if semantic_materiality == "stylistic":
        return "MINOR"

    # Semantic call unavailable / failed — proportion alone, capped below MAJOR
    return "MODERATE" if proportion_changed >= 0.50 else "MINOR"


# ── Critical-section check ─────────────────────────────────────────────────────

def is_critical_section(
    section_title: str | None,
    section_number: str | None,
    critical_patterns: list[str],
) -> bool:
    """Return True if the section matches any configured critical-section pattern.

    Patterns are case-insensitive substring matches against either
    ``section_title`` or ``section_number``.  An empty ``critical_patterns``
    list always returns False (no section is critical by default).

    Args:
        section_title:     Title text from ``document_sections.title`` (may be None).
        section_number:    Section number from ``document_sections.section_number``
                           (may be None).
        critical_patterns: Org-configured list from
                           ``org.settings["comparison"]["critical_sections"]``;
                           treated as case-insensitive substrings.

    Returns:
        True if any pattern matches either field; False otherwise.
    """
    if not critical_patterns:
        return False

    haystack_parts: list[str] = []
    if section_title:
        haystack_parts.append(section_title.lower())
    if section_number:
        haystack_parts.append(section_number.lower())
    if not haystack_parts:
        return False

    for pattern in critical_patterns:
        pat_lower = pattern.lower()
        for part in haystack_parts:
            if pat_lower in part:
                return True
    return False
