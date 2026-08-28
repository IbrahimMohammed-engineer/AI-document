"""
Golden chunk-quality regression tests (roadmap Phase 6 step 10).

Runs the four golden documents (hand-reviewed expected properties — the
regression baseline for every future chunker/detector change) through the
full pure pipeline:

    pages → build_page_lines → detect_sections → chunk_document

and asserts the reviewed expectations:

  policy_with_deep_toc   — correct 3-level tree, parents, chunk bounds
  table_heavy_sop        — contains_table, whole rows, section tree intact
  list_heavy_procedure   — list kept intact, contains_list flagged
  unstructured_scan      — zero sections (explicit non-error), page-level
                           chunks with NULL section

A deterministic word-count token counter keeps expectations stable; the
integration suite covers the real configured tokenizer end-to-end.

Docker is NOT required.
"""
from __future__ import annotations

import pytest

from app.ingestion.chunker import chunk_document
from app.ingestion.structure_detector import build_page_lines, detect_sections
from tests.fixtures.golden_documents import (
    list_heavy_procedure,
    policy_with_deep_toc,
    table_heavy_sop,
    unstructured_scan,
    word_counter,
)

# Small test band — the rules are identical, the fixtures stay readable
BAND = dict(
    count=word_counter,
    target_min_tokens=6,
    target_max_tokens=16,
    hard_max_tokens=24,
    overlap_tokens=2,
)


def _run(pages):
    pages_lines = [
        build_page_lines(page_number, text, None) for page_number, text in pages
    ]
    sections = detect_sections(pages_lines)
    plan = chunk_document(pages_lines, sections, **BAND)
    return sections, plan


# ─── 1. Policy with deep TOC ──────────────────────────────────────────────────

@pytest.mark.unit
def test_golden_policy_deep_toc_structure():
    pages = policy_with_deep_toc()
    sections, plan = _run(pages)

    numbers = [s.section_number for s in sections]
    assert numbers == ["1", "2", "3", "4", "4.1", "4.2", "4.2.1", "5"]
    levels = [s.level for s in sections]
    assert levels == [1, 1, 1, 1, 2, 2, 3, 1]

    by_number = {s.section_number: s for s in sections}
    # Parent links via the heading-level stack
    assert by_number["4.1"].parent_sort_order == by_number["4"].sort_order
    assert by_number["4.2"].parent_sort_order == by_number["4"].sort_order
    assert by_number["4.2.1"].parent_sort_order == by_number["4.2"].sort_order
    # Reading-order sort_order
    assert [s.sort_order for s in sections] == list(range(len(sections)))
    # start/end pages
    assert by_number["4"].start_page == 2
    assert by_number["4"].end_page == 3      # section 5 begins on page 3
    assert by_number["4.1"].end_page == 2    # 4.2 begins on page 2
    assert by_number["5"].end_page == 3      # last page


@pytest.mark.unit
def test_golden_policy_chunks_respect_bounds_and_sections():
    pages = policy_with_deep_toc()
    sections, plan = _run(pages)

    assert plan.chunks
    for chunk in plan.chunks:
        assert chunk.token_count <= 24                      # hard max
        assert chunk.start_page <= chunk.end_page
        assert chunk.heading_path                           # structured doc
    # Chunk sections appear in reading order and never mix sections
    section_orders = [c.section_sort_order for c in plan.chunks]
    assert section_orders == sorted(section_orders)
    # The deepest heading's content carries its full breadcrumb
    deep = [c for c in plan.chunks if c.section_number == "4.2.1"]
    assert deep and deep[0].heading_path == [
        "Approval Process", "Regulatory Review", "Substantiation File",
    ]


# ─── 2. Table-heavy SOP ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_golden_table_heavy_sop():
    pages = table_heavy_sop()
    sections, plan = _run(pages)

    assert [s.section_number for s in sections] == ["1", "2", "3"]
    table_chunks = [c for c in plan.chunks if c.contains_table]
    assert table_chunks, "the inspection matrix must be flagged contains_table"
    for chunk in table_chunks:
        for line in chunk.content.split("\n"):
            if "|" in line:
                assert len(line.split("|")) == 4  # whole rows only


# ─── 3. List-heavy procedure ──────────────────────────────────────────────────

@pytest.mark.unit
def test_golden_list_heavy_procedure():
    pages = list_heavy_procedure()
    sections, plan = _run(pages)

    assert [s.section_number for s in sections] == ["1", "2", "3"]
    list_chunks = [c for c in plan.chunks if c.contains_list]
    assert len(list_chunks) == 1, "the requirement list must stay together"
    content = list_chunks[0].content
    # All four items really are in the same chunk
    assert content.count("- ") == 4
    assert "- Administrator credentials" in content
    assert "- Incident channel opened" in content


# ─── 4. Unstructured scan ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_golden_unstructured_scan_degrades_to_page_level():
    pages = unstructured_scan()
    sections, plan = _run(pages)

    assert sections == []               # explicit no-structure state
    assert plan.chunks                  # chunking still works
    for chunk in plan.chunks:
        assert chunk.section_sort_order is None
        assert chunk.heading_path == []
        assert chunk.section_number is None
        assert not chunk.contains_table
        assert not chunk.contains_list
    # Content from both pages survives
    joined = "\n".join(c.content for c in plan.chunks)
    assert "quarterly facilities review" in joined
    assert "clearing the east drains" in joined
