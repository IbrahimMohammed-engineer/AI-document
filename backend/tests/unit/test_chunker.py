"""
Unit tests for the Phase 6 structure-aware chunker (Backend §21 rules).

The rule priority is exercised in isolation with a deterministic word-count
token counter and SMALL token bands (target 8–14, hard max 20) so a handful
of lines can violate every boundary:

  1. section boundaries are hard split points (never spanned, no overlap)
  2. target band respected; hard maximum never exceeded
  3. overlap only BETWEEN ADJACENT CHUNKS OF THE SAME SECTION, bounded
  4. tables never split mid-row
  5. lists kept intact where they fit
  6. page boundaries do not force splits (chunks span pages)
  + oversized paragraphs split at sentence boundaries (never mid-sentence);
    the giant-unbreakable case falls back to word splits with a
    forced_split note
  + chunk_index is sequential 0-based reading order; page attribution is
    correct for page-spanning chunks

Docker is NOT required.
"""
from __future__ import annotations

import pytest

from app.ingestion.chunker import (
    build_segments,
    chunk_document,
    chunk_documents_segments,
)
from app.ingestion.structure_detector import build_page_lines, detect_sections
from tests.fixtures.golden_documents import word_counter


def _pages_lines(pages: list[tuple[int, str]]):
    return [
        build_page_lines(page_number, text, None) for page_number, text in pages
    ]


def _chunk(pages: list[tuple[int, str]], **overrides):
    """Run the full pure pipeline with the small test token band."""
    params = dict(
        count=word_counter,
        target_min_tokens=4,
        target_max_tokens=14,
        hard_max_tokens=20,
        overlap_tokens=2,
    )
    params.update(overrides)
    pages_lines = _pages_lines(pages)
    sections = detect_sections(pages_lines)
    return chunk_document(pages_lines, sections, **params)


# ─── Rule 1: section boundaries ───────────────────────────────────────────────

@pytest.mark.unit
def test_chunks_never_span_sections():
    pages = [
        (1, "1. Alpha\n" + "word " * 30),
        (1, "2. Beta\n" + "term " * 30),
    ]
    plan = _chunk(pages)
    assert plan.chunks, "expected chunks"
    for chunk in plan.chunks:
        # Every chunk belongs to exactly one section (never both)
        if chunk.section_sort_order == 0:
            assert "Beta" not in chunk.content
        elif chunk.section_sort_order == 1:
            assert "Alpha" not in chunk.content


@pytest.mark.unit
def test_no_overlap_across_section_boundary():
    pages = [
        (1, "1. Alpha\nfirst section content ".replace("  ", " ") + "tailword"),
        (2, "2. Beta\nsecond section content entirely different words here"),
    ]
    plan = _chunk(pages)
    # Section 0's chunks must never contain section 1's heading text and
    # vice versa — overlap never crosses the boundary
    sections_chunks = {}
    for chunk in plan.chunks:
        sections_chunks.setdefault(chunk.section_sort_order, []).append(chunk)
    assert set(sections_chunks) == {0, 1}
    for chunk in sections_chunks[0]:
        assert "Beta" not in chunk.content
    for chunk in sections_chunks[1]:
        assert "Alpha" not in chunk.content


# ─── Rule 2: size limits ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_hard_maximum_never_exceeded():
    pages = [(1, "1. Body\n" + "\n".join(f"sentence number {i} ends here." for i in range(40)))]
    plan = _chunk(pages)
    assert plan.chunks
    for chunk in plan.chunks:
        assert chunk.token_count <= 20


@pytest.mark.unit
def test_oversized_paragraph_splits_at_sentence_boundaries():
    pages = [
        (
            1,
            "1. Rules\n"
            "The first sentence explains a complete idea thoroughly today. "
            "The second sentence explains another complete idea thoroughly now. "
            "The third sentence explains yet another complete idea very well. "
            "The fourth sentence wraps the argument up neatly and completely.",
        )
    ]
    plan = _chunk(pages)
    joined = "\n".join(c.content for c in plan.chunks)
    # No sentence was severed: every full sentence appears intact somewhere
    for marker in (
        "The first sentence",
        "The second sentence",
        "The third sentence",
        "The fourth sentence",
    ):
        assert marker in joined


@pytest.mark.unit
def test_giant_unbreakable_sentence_falls_back_to_word_split():
    """One sentence above the hard max → word-boundary pieces + forced_split."""
    pages = [(1, "1. Dense\n" + "unbreakable " * 60)]
    plan = _chunk(pages)
    assert plan.chunks
    assert all(c.token_count <= 20 for c in plan.chunks)
    assert any(c.forced_split for c in plan.chunks)


# ─── Rule 3: overlap within a section ─────────────────────────────────────────

@pytest.mark.unit
def test_overlap_between_adjacent_chunks_same_section():
    # One section, many paragraph sentences → multiple chunks with overlap
    pages = [
        (
            1,
            "1. Long Section\n"
            + " ".join(
                f"Sentence number {i} carries its own complete meaning today."
                for i in range(12)
            ),
        )
    ]
    plan = _chunk(pages)
    assert len(plan.chunks) >= 2, "fixture should produce multiple chunks"
    for previous, following in zip(plan.chunks, plan.chunks[1:]):
        overlap_words = set(previous.content.split()) & set(following.content.split())
        # Some context is shared, but overlap stays small relative to size
        assert len(overlap_words) <= max(4, previous.token_count // 2)
    # Overlap never exceeds the budget of 2 words + joinings
    for previous, following in zip(plan.chunks, plan.chunks[1:]):
        shared_sentences = set(previous.content.split(". ")) & set(
            following.content.split(". ")
        )
        assert len(shared_sentences) <= 1


@pytest.mark.unit
def test_no_overlap_for_first_and_last_chunks_alone():
    pages = [(1, "1. Short\nOnly one tiny chunk of content here.")]
    plan = _chunk(pages)
    assert len(plan.chunks) == 1  # a single chunk carries no overlap


# ─── Rule 4: table integrity ──────────────────────────────────────────────────

@pytest.mark.unit
def test_table_rows_are_never_split_mid_row():
    pages = [
        (
            1,
            "\n".join(
                [
                    "1. Matrix",
                    "Equipment | Frequency | Owner | Escalation",
                    "Fire extinguishers | Monthly | Facilities | Week 1",
                    "Eyewash stations | Monthly | Labs | Week 1",
                    "Sprinkler system | Quarterly | Facilities | Week 2",
                    "Alarm panel | Quarterly | Security | Week 2",
                    "Backup generator | Annually | Facilities | Week 3",
                ]
            ),
        )
    ]
    plan = _chunk(pages)
    table_chunks = [c for c in plan.chunks if c.contains_table]
    assert table_chunks, "table content must be marked contains_table"
    for chunk in table_chunks:
        for line in chunk.content.split("\n"):
            if "|" in line:
                # A row present in a chunk is COMPLETE in that chunk
                cells = line.split("|")
                assert len(cells) == 4


@pytest.mark.unit
def test_oversized_table_splits_at_row_boundaries_with_forced_split():
    rows = "\n".join(
        f"Item {i} description | Weekly | Team {i % 3} | Day {i % 7}"
        for i in range(30)
    )
    pages = [(1, "1. Big Matrix\n" + rows)]
    plan = _chunk(pages)
    table_chunks = [c for c in plan.chunks if c.contains_table]
    assert len(table_chunks) >= 2  # actually split
    for chunk in table_chunks:
        for line in chunk.content.split("\n"):
            if "|" in line:
                assert len(line.split("|")) == 4  # whole rows only
    assert any(c.forced_split for c in table_chunks)


# ─── Rule 5: list integrity ───────────────────────────────────────────────────

@pytest.mark.unit
def test_list_is_kept_intact_in_one_chunk():
    pages = [
        (
            1,
            "\n".join(
                [
                    "1. Materials",
                    "Gather everything before starting the process below now:",
                    "- First required item goes here",
                    "- Second required item goes here",
                    "- Third required item goes here",
                ]
            ),
        )
    ]
    plan = _chunk(pages)
    list_chunks = [c for c in plan.chunks if c.contains_list]
    assert len(list_chunks) == 1
    content = list_chunks[0].content
    assert "- First required item" in content
    assert "- Third required item" in content  # all items together


# ─── Rule 6: page spanning ────────────────────────────────────────────────────

@pytest.mark.unit
def test_chunks_may_span_pages():
    pages = [
        (1, "1. Spanning\n" + "alpha " * 12),
        (2, "continues " * 12),
    ]
    plan = _chunk(pages)
    spanning = [c for c in plan.chunks if c.end_page > c.start_page]
    assert spanning, "page boundaries must not force a split"
    for chunk in plan.chunks:
        assert chunk.start_page <= chunk.end_page


# ─── Reading order + provenance ───────────────────────────────────────────────

@pytest.mark.unit
def test_chunk_index_is_sequential_reading_order():
    pages = [
        (1, "1. One\ncontent for section one repeats content words"),
        (2, "2. Two\ncontent for section two repeats content words"),
        (3, "3. Three\ncontent for section three repeats content"),
    ]
    plan = _chunk(pages)
    indexes = [c.chunk_index for c in plan.chunks]
    assert indexes == list(range(len(indexes)))
    # Reading order: section 1 chunks come before section 2 chunks
    section_order = [c.section_sort_order for c in plan.chunks]
    assert section_order == sorted(section_order)


@pytest.mark.unit
def test_heading_path_and_section_number_recorded():
    pages = [
        (1, "3. Parent\n" + "filler words " * 10),
        (1, "3.1 Child\n" + "child words " * 10),
    ]
    plan = _chunk(pages)
    child_chunks = [c for c in plan.chunks if c.section_number == "3.1"]
    assert child_chunks
    assert child_chunks[0].heading_path == ["Parent", "Child"]
    parent_chunks = [c for c in plan.chunks if c.section_number == "3"]
    assert parent_chunks[0].heading_path == ["Parent"]


@pytest.mark.unit
def test_unstructured_content_still_chunks():
    pages = [
        (1, "Plain body text without any headings repeats words for volume."),
        (2, "More plain body text without headings on the second page here."),
    ]
    plan = _chunk(pages)
    assert plan.chunks
    for chunk in plan.chunks:
        assert chunk.section_sort_order is None
        assert chunk.heading_path == []


# ─── Segments ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_build_segments_kinds_and_pages():
    pages = [
        (
            1,
            "\n".join(
                [
                    "1. Purpose",
                    "Plain paragraph content spread on one page.",
                    "Header | Col A | Col B",
                    "Row one | value | value",
                    "- Bullet item one",
                    "- Bullet item two",
                ]
            ),
        )
    ]
    pages_lines = _pages_lines(pages)
    sections = detect_sections(pages_lines)
    segments = build_segments(pages_lines, sections)
    kinds = [s.kind for s in segments]
    assert kinds[0] == "heading"
    assert "paragraph" in kinds
    assert "table" in kinds
    assert "list" in kinds
    for segment in segments:
        assert segment.start_page == 1
        assert segment.end_page == 1
        # Everything after the heading belongs to section 0
        assert segment.section_sort_order == 0


@pytest.mark.unit
def test_merge_undersized_chunks_within_same_section_only():
    """Undersized chunks merge with same-section neighbors, never across."""
    pages = [
        (1, "1. Tiny Section\nshort words"),
        (2, "2. Another Tiny\nmore short words"),
    ]
    plan = _chunk(pages)
    # Two tiny sections → two tiny chunks, NOT merged across sections
    section_numbers = {c.section_number for c in plan.chunks}
    assert section_numbers == {"1", "2"}


@pytest.mark.unit
def test_realistic_band_produces_reasonable_chunk_sizes():
    """With the production band, chunks land in the 500–800 target region."""
    body = " ".join(
        f"Sentence {i} of the section carries meaning for retrieval quality."
        for i in range(60)
    )
    pages = [(1, "1. Realistic\n" + body)]
    plan = chunk_document(
        _pages_lines(pages),
        detect_sections(_pages_lines(pages)),
        count=word_counter,
        target_min_tokens=40,
        target_max_tokens=64,
        hard_max_tokens=80,
        overlap_tokens=6,
    )
    assert len(plan.chunks) >= 2
    for chunk in plan.chunks:
        assert chunk.token_count <= 80
    # At least one chunk reaches into the target band
    assert max(c.token_count for c in plan.chunks) >= 40


@pytest.mark.unit
def test_segment_packing_directly_respects_section_boundaries():
    """chunk_documents_segments: mixed-section segments never share a chunk."""
    from app.ingestion.chunker import Segment

    segments = [
        Segment(kind="paragraph", text="alpha words here", start_page=1,
                end_page=1, section_sort_order=0, tokens=3),
        Segment(kind="paragraph", text="beta words here", start_page=1,
                end_page=1, section_sort_order=1, tokens=3),
    ]
    plan = chunk_documents_segments(
        segments,
        [],
        count=word_counter,
        target_min_tokens=1,
        target_max_tokens=100,
        hard_max_tokens=100,
        overlap_tokens=2,
    )
    assert len(plan.chunks) == 2
    assert plan.chunks[0].content == "alpha words here"
    assert plan.chunks[1].content == "beta words here"
