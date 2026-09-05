"""
Unit tests — summary sampling rules (Phase 14, plan §5.6/§8.1).

Covers: the full-document (no sampling) case; the long-document case
asserting EVERY top-level section contributes at least one chunk (the
"section coverage assertion" the roadmap names explicitly); the zero-section
(unstructured document) fallback; and a section with zero directly-tagged
chunks falling back to the page-range method.
"""
from __future__ import annotations

import pytest

from app.domain.summary_rules import (
    ChunkRef,
    SamplingDisclosure,
    SectionRef,
    select_representative_chunks,
)

BUDGET = 100
PER_SECTION = 1


def _section(sid: str, order: int, parent: str | None = None, **kw) -> SectionRef:
    return SectionRef(
        id=sid,
        parent_section_id=parent,
        sort_order=order,
        start_page=kw.get("start_page", 1),
        end_page=kw.get("end_page", 1),
    )


def _chunk(cid: str, index: int, tokens: int, section: str | None, page: int = 1) -> ChunkRef:
    return ChunkRef(
        id=cid, section_id=section, chunk_index=index,
        token_count=tokens, page_number=page,
    )


@pytest.mark.unit
class TestSelectRepresentativeChunks:

    def test_short_document_returns_all_chunks_no_sampling(self):
        sections = [_section("s1", 0)]
        chunks = [
            _chunk("c0", 0, 10, "s1"),
            _chunk("c1", 1, 10, "s1"),
        ]
        selected, disclosure = select_representative_chunks(
            sections, chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=PER_SECTION,
        )
        assert selected == ["c0", "c1"]
        assert disclosure == SamplingDisclosure(
            sampled=False, strategy="full",
            included_section_ids=[], excluded_section_count=0,
        )

    def test_section_coverage_every_top_level_section_contributes(self):
        # Long document: 3 top-level sections x 3 chunks x 40 tokens = 360 > 100
        sections = [
            _section("top1", 0), _section("top2", 1), _section("top3", 2),
            _section("sub2a", 3, parent="top2"),  # nested — belongs to top2
        ]
        chunks = []
        index = 0
        for top in ("top1", "top2", "top3"):
            for _ in range(3):
                chunks.append(_chunk(f"{top}-c{index}", index, 40, top))
                index += 1
        # A nested-section chunk must count toward its top-level ancestor.
        chunks.append(_chunk("sub-chunk", index, 40, "sub2a"))

        selected, disclosure = select_representative_chunks(
            sections, chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=PER_SECTION,
        )
        assert disclosure.sampled is True
        assert disclosure.strategy == "section_diverse"
        # THE section coverage assertion (roadmap): every top-level section
        # contributes at least one chunk.
        assert set(disclosure.included_section_ids) == {"top1", "top2", "top3"}
        assert disclosure.excluded_section_count == 0
        # One median chunk per top-level section.
        assert len(selected) == 3
        # Selected ids preserve original reading order.
        positions = [c.chunk_index for c in chunks if c.id in set(selected)]
        assert positions == sorted(positions)

    def test_zero_sections_falls_back_to_evenly_spaced(self):
        chunks = [_chunk(f"c{i}", i, 30, None) for i in range(10)]
        selected, disclosure = select_representative_chunks(
            [], chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=PER_SECTION,
        )
        assert disclosure.sampled is True
        assert disclosure.strategy == "section_diverse"
        assert disclosure.included_section_ids == []
        assert 0 < len(selected) < 10
        # Reading order preserved.
        positions = [c.chunk_index for c in chunks if c.id in set(selected)]
        assert positions == sorted(positions)

    def test_untagged_section_uses_page_range_fallback(self):
        # "Orphan" top-level section has NO section_id-tagged chunks but owns
        # pages 2-3; another section has tagged chunks.
        sections = [_section("tagged", 0), _section("orphan", 1, start_page=2, end_page=3)]
        chunks = [
            _chunk("t0", 0, 60, "tagged", page=1),
            _chunk("p2", 1, 60, None, page=2),
            _chunk("p3", 2, 60, None, page=3),
        ]
        selected, disclosure = select_representative_chunks(
            sections, chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=PER_SECTION,
        )
        assert disclosure.sampled is True
        # Both sections contributed — the orphan via the page-range fallback.
        assert set(disclosure.included_section_ids) == {"tagged", "orphan"}
        assert "p2" in selected or "p3" in selected

    def test_median_position_selected_not_first(self):
        sections = [_section("top", 0)]
        chunks = [
            _chunk(f"c{i}", i, 60, "top") for i in range(5)
        ]
        selected, _ = select_representative_chunks(
            sections, chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=PER_SECTION,
        )
        # The MIDDLE chunk of five (index 2) is the median representative.
        assert selected == ["c2"]

    def test_multiple_chunks_per_section_honored(self):
        sections = [_section("top1", 0), _section("top2", 1)]
        chunks = [
            _chunk("a0", 0, 60, "top1"), _chunk("a1", 1, 60, "top1"),
            _chunk("b0", 2, 60, "top2"), _chunk("b1", 3, 60, "top2"),
        ]
        selected, disclosure = select_representative_chunks(
            sections, chunks,
            summary_context_token_budget=BUDGET,
            summary_sampling_chunks_per_section=2,
        )
        assert len(selected) == 4  # 2 per section
        assert set(disclosure.included_section_ids) == {"top1", "top2"}
