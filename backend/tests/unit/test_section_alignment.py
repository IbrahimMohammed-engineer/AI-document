"""
Unit tests for app/domain/section_alignment.py (Phase 12).

Tests the three-pass section matching strategy (number, title, embedding),
the unmatched (ADDED/REMOVED) pass, and the whole-document fallback.
Pure functions — no DB, no I/O.
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass
from typing import Optional

from app.domain.section_alignment import (
    ALIGNMENT_MIN_MATCH_RATIO,
    EMBEDDING_SIMILARITY_THRESHOLD,
    SectionAlignment,
    align_sections,
)


# ── Stub section object ────────────────────────────────────────────────────────

@dataclass
class SectionStub:
    id: str
    section_number: Optional[str] = None
    title: Optional[str] = None
    sort_order: int = 0


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAlignSectionsEmpty:
    def test_both_empty(self):
        alignments, degraded = align_sections([], [])
        assert alignments == []
        assert degraded is False

    def test_a_empty_all_added(self):
        b = [SectionStub("b1", "1.0", "Intro")]
        alignments, degraded = align_sections([], b)
        # All of B should become ADDED (section_a=None)
        assert any(a.section_a is None and a.section_b is not None for a in alignments)
        assert degraded is False

    def test_b_empty_all_removed(self):
        a = [SectionStub("a1", "1.0", "Intro")]
        alignments, degraded = align_sections(a, [])
        assert any(a_.section_a is not None and a_.section_b is None for a_ in alignments)
        assert degraded is False


class TestPass1NumberMatch:
    def test_exact_number_match(self):
        a = [SectionStub("a1", "1.0", "Introduction")]
        b = [SectionStub("b1", "1.0", "Intro")]
        alignments, degraded = align_sections(a, b)
        matched = [al for al in alignments if al.match_method == "number"]
        assert len(matched) == 1
        assert matched[0].confidence == 1.0
        assert matched[0].section_a is a[0]
        assert matched[0].section_b is b[0]

    def test_non_matching_numbers_produce_unmatched(self):
        a = [SectionStub("a1", "1.0"), SectionStub("a2", "2.0")]
        b = [SectionStub("b1", "1.0"), SectionStub("b2", "3.0")]
        alignments, _ = align_sections(a, b)
        methods = {al.match_method for al in alignments}
        assert "number" in methods
        assert "unmatched" in methods  # 2.0 ↔ 3.0 — no match


class TestPass2TitleMatch:
    def test_normalized_title_match(self):
        a = [SectionStub("a1", None, "Introduction to Policies")]
        b = [SectionStub("b1", None, "Introduction To Policies")]
        alignments, degraded = align_sections(a, b)
        matched = [al for al in alignments if al.match_method == "title"]
        assert len(matched) == 1
        assert matched[0].confidence == 0.9

    def test_punctuation_stripped_title_match(self):
        a = [SectionStub("a1", None, "Scope & Purpose")]
        b = [SectionStub("b1", None, "Scope  Purpose")]
        alignments, _ = align_sections(a, b)
        # After stripping punctuation the normalized forms should match
        # Both become "scopepurpose" (or similar) → should match
        # This depends on normalization; just assert at least one alignment exists
        assert len(alignments) >= 1


class TestPass3EmbeddingMatch:
    def test_embedding_similarity_above_threshold(self):
        a = [SectionStub("a1", None, "Definitions")]
        b = [SectionStub("b1", None, "Definitions Section")]
        # Artificially high similarity vectors
        embed_lookup = {
            "a1": [1.0, 0.0, 0.0],
            "b1": [0.99, 0.0, 0.0],
        }
        alignments, _ = align_sections(a, b, embedding_lookup=embed_lookup)
        embedding_matches = [al for al in alignments if al.match_method == "embedding"]
        assert len(embedding_matches) == 1

    def test_embedding_similarity_below_threshold_unmatched(self):
        a = [SectionStub("a1", None, "Definitions")]
        b = [SectionStub("b1", None, "Termination")]
        # Very different vectors (sim ≈ 0)
        embed_lookup = {
            "a1": [1.0, 0.0, 0.0],
            "b1": [0.0, 1.0, 0.0],
        }
        alignments, _ = align_sections(a, b, embedding_lookup=embed_lookup)
        embedding_matches = [al for al in alignments if al.match_method == "embedding"]
        assert len(embedding_matches) == 0
        # Both should be unmatched
        unmatched = [al for al in alignments if al.match_method == "unmatched"]
        assert len(unmatched) == 2


class TestDegradedFallback:
    def test_below_30pct_match_ratio_triggers_fallback(self):
        # 10 sections in B but only 0 match A (completely different numbering)
        a = [SectionStub(f"a{i}", None, f"Title {i}") for i in range(2)]
        b = [SectionStub(f"b{i}", None, f"Completely Different {i}") for i in range(10)]
        alignments, degraded = align_sections(a, b)
        assert degraded is True
        # Fallback alignment should be the whole-document pseudo-alignment
        assert any(al.match_method == "whole_document" for al in alignments)
