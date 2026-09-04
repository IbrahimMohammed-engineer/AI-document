"""
Section alignment for document comparison — pure domain, no I/O.

Takes the sorted section lists for two document versions and produces a
mapping of (section_a, section_b) pairs for downstream text comparison.
Unmatched sections become ADDED (only in B) or REMOVED (only in A).

Matching passes (§9.4 — first match wins per section):
  1. Exact ``section_number`` match             → confidence 1.0
  2. Normalized-title match (lowercase, strip)  → confidence 0.9
  3. Title-embedding cosine similarity (>= 0.86 threshold)
     — caller pre-computes a {section_id: vector} mapping to keep this
     module pure (no provider calls; domain/*.py has no I/O per convention)
  4. Unmatched: ADDED (B only) or REMOVED (A only)

Pathological restructuring: if fewer than 30% of version B's sections
achieve any match, returns a single pseudo-alignment covering the whole
document text (``alignment_degraded=True`` in the result) — §9.4 fallback.

See PHASE-12-IMPLEMENTATION-PLAN.md §9.4, §11 Task 2.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SectionAlignment:
    """One aligned (or unmatched) section pair.

    - Both sides present  → matched pair for text/semantic comparison.
    - ``section_a = None`` → ADDED (exists only in B).
    - ``section_b = None`` → REMOVED (exists only in A).
    """
    section_a: Any | None          # DocumentSection ORM instance or stub
    section_b: Any | None
    confidence: float              # 1.0 = exact number, 0.9 = title, 0.0–1.0 = cosine
    match_method: str              # "number" | "title" | "embedding" | "unmatched"


# Threshold for embedding-similarity match (plan-authored — tune as needed)
EMBEDDING_SIMILARITY_THRESHOLD = 0.86
# Minimum fraction of B's sections that must match before falling back to
# whole-document comparison (plan-authored, §9.4)
ALIGNMENT_MIN_MATCH_RATIO = 0.30
# The degraded-fallback targets "pathological restructuring" of a real
# document structure.  When either side has fewer sections than this, the
# whole-document pseudo-alignment would only destroy information (every
# section is legitimately ADDED/REMOVED) — emit unmatched rows instead
# (plan §20 edge cases: empty side / tiny documents).
ALIGNMENT_MIN_SECTIONS_FOR_DEGRADE = 3


# ── Public API ────────────────────────────────────────────────────────────────

def align_sections(
    sections_a: list[Any],
    sections_b: list[Any],
    *,
    embedding_lookup: dict[str, list[float]] | None = None,
) -> tuple[list[SectionAlignment], bool]:
    """Align sections from version A with sections from version B.

    Args:
        sections_a:        All DocumentSection rows for version A, sorted by
                           ``sort_order`` (or any consistent order).
        sections_b:        All DocumentSection rows for version B.
        embedding_lookup:  Optional mapping of section_id → embedding vector
                           (precomputed by the caller to keep this function
                           pure).  If None, pass 3 will be skipped.

    Returns:
        (alignments, alignment_degraded)
        - alignments: List of SectionAlignment items (matched + unmatched).
        - alignment_degraded: True when fewer than 30% of B's sections matched
          (whole-document fallback triggered).
    """
    if not sections_a and not sections_b:
        return [], False

    # --- Pass 1: exact section_number match ──────────────────────────────────
    unmatched_a = list(sections_a)
    unmatched_b = list(sections_b)
    alignments: list[SectionAlignment] = []

    def _number(sec: Any) -> str | None:
        return getattr(sec, "section_number", None) or None

    b_by_number: dict[str, Any] = {}
    for sec in unmatched_b:
        n = _number(sec)
        if n:
            b_by_number.setdefault(n, sec)

    still_unmatched_a: list[Any] = []
    still_unmatched_b_ids: set[str] = {getattr(s, "id", id(s)) for s in unmatched_b}

    for sec_a in unmatched_a:
        n = _number(sec_a)
        if n and n in b_by_number:
            sec_b = b_by_number.pop(n)
            alignments.append(SectionAlignment(sec_a, sec_b, 1.0, "number"))
            still_unmatched_b_ids.discard(getattr(sec_b, "id", id(sec_b)))
        else:
            still_unmatched_a.append(sec_a)

    unmatched_b_pass2 = [s for s in unmatched_b if getattr(s, "id", id(s)) in still_unmatched_b_ids]

    # --- Pass 2: normalized title match ──────────────────────────────────────
    def _norm_title(sec: Any) -> str:
        title = getattr(sec, "title", None) or ""
        return re.sub(r"[^a-z0-9]", "", title.lower())

    b_by_norm_title: dict[str, Any] = {}
    for sec in unmatched_b_pass2:
        nt = _norm_title(sec)
        if nt:
            b_by_norm_title.setdefault(nt, sec)

    still_unmatched_a2: list[Any] = []
    used_b_ids_p2: set[str] = set()

    for sec_a in still_unmatched_a:
        nt = _norm_title(sec_a)
        if nt and nt in b_by_norm_title:
            sec_b = b_by_norm_title.pop(nt)
            alignments.append(SectionAlignment(sec_a, sec_b, 0.9, "title"))
            used_b_ids_p2.add(getattr(sec_b, "id", id(sec_b)))
        else:
            still_unmatched_a2.append(sec_a)

    unmatched_b_pass3 = [
        s for s in unmatched_b_pass2
        if getattr(s, "id", id(s)) not in used_b_ids_p2
    ]

    # --- Pass 3: embedding cosine similarity (only if lookup provided) ───────
    still_unmatched_a3: list[Any] = list(still_unmatched_a2)
    unmatched_b_pass4: list[Any] = list(unmatched_b_pass3)

    if embedding_lookup and still_unmatched_a2 and unmatched_b_pass3:
        paired_a: set[int] = set()
        paired_b: set[int] = set()

        # Build cosine-score matrix for all remaining pairs
        scored: list[tuple[float, int, int]] = []
        for ia, sec_a in enumerate(still_unmatched_a2):
            vec_a = embedding_lookup.get(getattr(sec_a, "id", ""))
            if not vec_a:
                continue
            for ib, sec_b in enumerate(unmatched_b_pass3):
                vec_b = embedding_lookup.get(getattr(sec_b, "id", ""))
                if not vec_b:
                    continue
                sim = _cosine(vec_a, vec_b)
                if sim >= EMBEDDING_SIMILARITY_THRESHOLD:
                    scored.append((sim, ia, ib))

        # Greedy best-match-first (highest similarity first)
        scored.sort(reverse=True)
        for sim, ia, ib in scored:
            if ia in paired_a or ib in paired_b:
                continue
            sec_a = still_unmatched_a2[ia]
            sec_b = unmatched_b_pass3[ib]
            alignments.append(SectionAlignment(sec_a, sec_b, sim, "embedding"))
            paired_a.add(ia)
            paired_b.add(ib)

        still_unmatched_a3 = [s for i, s in enumerate(still_unmatched_a2) if i not in paired_a]
        unmatched_b_pass4 = [s for i, s in enumerate(unmatched_b_pass3) if i not in paired_b]

    # --- Check alignment quality before adding unmatched rows ────────────────
    total_b = len(sections_b)
    matched_count = len(alignments)
    alignment_degraded = False

    if (
        sections_a
        and total_b >= ALIGNMENT_MIN_SECTIONS_FOR_DEGRADE
        and matched_count / total_b < ALIGNMENT_MIN_MATCH_RATIO
    ):
        # Pathological restructuring — fall back to whole-document comparison
        alignment_degraded = True
        return _whole_document_fallback(sections_a, sections_b), alignment_degraded

    # --- Pass 4: unmatched sections become ADDED / REMOVED ───────────────────
    for sec_a in still_unmatched_a3:
        alignments.append(SectionAlignment(sec_a, None, 0.0, "unmatched"))  # REMOVED

    for sec_b in unmatched_b_pass4:
        alignments.append(SectionAlignment(None, sec_b, 0.0, "unmatched"))  # ADDED

    return alignments, alignment_degraded


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cosine(vec_a: list[float], vec_b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(x * x for x in vec_a))
    mag_b = math.sqrt(sum(x * x for x in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _whole_document_fallback(
    sections_a: list[Any],
    sections_b: list[Any],
) -> list[SectionAlignment]:
    """Single pseudo-alignment covering both whole documents' text."""
    # Create one pseudo-section object carrying all text per side.
    # The worker will concatenate actual chunk text from both version's chunks
    # when alignment_degraded=True, so we just signal with None+None = use all.
    return [SectionAlignment(None, None, 0.0, "whole_document")]
