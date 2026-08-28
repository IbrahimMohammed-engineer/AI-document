"""
Unit tests for the Phase 6 heuristic structure detector (Backend §20).

Covers:
  - numbering patterns: decimal multi-level ("4.2"), single-level ("1."),
    "Section 4", Roman ("IV."), "Appendix A"
  - the heading-level stack algorithm: a new heading at level N becomes a
    child of the most recent still-open level N−1 heading
  - list items that share the "1. " shape are NOT headings
  - end_page derivation (next same-or-higher-level heading / document end)
  - font-signal fallback (size vs body median, bold) and its suppression
    once numbering is in play
  - all-caps fallback with its strict guards (repeat furniture, solo lines)
  - the no-structure state: zero sections, explicitly non-error

Docker is NOT required.
"""
from __future__ import annotations

import pytest

from app.ingestion.structure_detector import (
    LineFont,
    LineInfo,
    PdfFontInfo,
    build_page_lines,
    detect_sections,
    extract_pdf_font_info,
    normalize_line,
)


def _lines_of(pages: list[tuple[int, str]]) -> list[list[LineInfo]]:
    return [build_page_lines(page_number, text, None) for page_number, text in pages]


def _flat(pages: list[tuple[int, str]]) -> list[LineInfo]:
    return [line for page in _lines_of(pages) for line in page]


# ─── Numbering patterns ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_decimal_numbering_multi_level():
    pages = [
        (1, "4. Approval Process\nBody text follows here."),
        (1, "4.2 Regulatory Review\nBody text follows here."),
    ]
    sections = detect_sections(_lines_of(pages))
    assert [s.section_number for s in sections] == ["4", "4.2"]
    assert [s.level for s in sections] == [1, 2]
    assert [s.title for s in sections] == ["Approval Process", "Regulatory Review"]
    # Stack algorithm: 4.2's parent is 4 (most recent open level 1)
    assert sections[1].parent_sort_order == sections[0].sort_order


@pytest.mark.unit
def test_deep_nesting_three_levels():
    pages = [
        (
            1,
            "\n".join(
                [
                    "4. Approval Process",
                    "4.1 Marketing Review",
                    "4.2 Regulatory Review",
                    "4.2.1 Substantiation File",
                    "4.2.2 Evidence Retention",
                ]
            ),
        )
    ]
    sections = detect_sections(_lines_of(pages))
    assert [s.section_number for s in sections] == [
        "4", "4.1", "4.2", "4.2.1", "4.2.2",
    ]
    assert [s.level for s in sections] == [1, 2, 2, 3, 3]
    by_sort = {s.sort_order: s for s in sections}
    assert by_sort[1].parent_sort_order == 0   # 4.1 → 4
    assert by_sort[2].parent_sort_order == 0   # 4.2 → 4 (popped 4.1)
    assert by_sort[3].parent_sort_order == 2   # 4.2.1 → 4.2
    assert by_sort[4].parent_sort_order == 2   # 4.2.2 → 4.2 (popped 4.2.1)


@pytest.mark.unit
def test_level_jump_nests_under_most_recent_open_lower_level():
    """A level-3 heading with no level-2 parent nests under the open level 1."""
    pages = [(1, "2. Background\n2.1.1 Odd Deep Heading")]
    sections = detect_sections(_lines_of(pages))
    assert [s.level for s in sections] == [1, 3]
    assert sections[1].parent_sort_order == sections[0].sort_order


@pytest.mark.unit
def test_section_word_and_appendix_patterns():
    pages = [
        (1, "Section 4 Approval Process\nSection 5 Records\nAppendix A Glossary\nAppendix B.2 Forms"),
    ]
    sections = detect_sections(_lines_of(pages))
    assert [(s.section_number, s.level) for s in sections] == [
        ("4", 1), ("5", 1), ("Appendix A", 1), ("Appendix B.2", 2),
    ]


@pytest.mark.unit
def test_roman_numbering():
    pages = [(1, "IV. Scope\nV. Definitions")]
    sections = detect_sections(_lines_of(pages))
    assert [s.section_number for s in sections] == ["IV", "V"]
    assert all(s.level == 1 for s in sections)


@pytest.mark.unit
def test_single_level_decimal_long_line_is_a_list_item_not_a_heading():
    pages = [
        (
            1,
            "1. Install the mounting bracket assembly securely before any further steps are attempted by technicians",
            # 17 words — list item shape, not a heading
        )
    ]
    assert detect_sections(_lines_of(pages)) == []


@pytest.mark.unit
def test_numbering_suppresses_caps_and_font_fallbacks():
    """Once real numbered headings exist, caps lines don't become sections."""
    pages = [
        (1, "1. Purpose\nINTRODUCTION NOTES HERE\nMore plain body text here."),
        (2, "2. Scope\nEverything after the first numbered heading."),
    ]
    sections = detect_sections(_lines_of(pages))
    assert [s.section_number for s in sections] == ["1", "2"]
    assert all(s.matched_by == "numbering" for s in sections)


# ─── end_page derivation ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_end_page_is_next_same_or_higher_heading_start():
    pages = [
        (1, "1. First\nbody"),
        (2, "1.1 Nested\nbody"),
        (3, "2. Second\nbody"),
        (4, "trailing body with no heading"),
    ]
    sections = detect_sections(_lines_of(pages))
    by_number = {s.section_number: s for s in sections}
    # Section 1 spans until section 2 begins (page 3)
    assert by_number["1"].end_page == 3
    # 1.1 (level 2) ends where the next level ≤ 2 heading begins — page 3
    assert by_number["1.1"].end_page == 3
    # Last section runs to the document's last page
    assert by_number["2"].end_page == 4


# ─── Font-signal fallback ─────────────────────────────────────────────────────

def _font_info_with_body(
    page: int, lines: dict[str, tuple[float, bool]], body: float
) -> PdfFontInfo:
    info = PdfFontInfo()
    info.lines[page] = {
        normalize_line(text): LineFont(size=size, bold=bold)
        for text, (size, bold) in lines.items()
    }
    info.body_sizes[page] = body
    return info


@pytest.mark.unit
def test_font_heading_large_and_bold_lines_without_numbering():
    pages = [(1, "Introduction Overview\nRegular body text continues here.")]
    font_info = _font_info_with_body(
        1,
        {
            "Introduction Overview": (18.0, False),   # ≥ 1.4× body → level 1
            "Regular body text continues here.": (12.0, False),
        },
        body=12.0,
    )
    # Lines must be built WITH the font info so the signals attach
    pages_lines = [build_page_lines(page_number, text, font_info)
                   for page_number, text in pages]
    sections = detect_sections(pages_lines, font_info)
    assert len(sections) == 1
    assert sections[0].matched_by == "font"
    assert sections[0].level == 1
    assert sections[0].title == "Introduction Overview"


@pytest.mark.unit
def test_font_heading_ignores_sentence_shaped_lines():
    pages = [(1, "This is just a large sentence that keeps going on and on.")]
    font_info = _font_info_with_body(
        1, {"This is just a large sentence that keeps going on and on.": (20.0, True)},
        body=12.0,
    )
    assert detect_sections(_lines_of(pages), font_info) == []


@pytest.mark.unit
def test_font_lookup_miss_degrades_to_no_structure():
    pages = [(1, "Some Title Line\nBody text without any signals.")]
    font_info = _font_info_with_body(
        1, {"unrelated span text": (20.0, True)}, body=12.0
    )
    assert detect_sections(_lines_of(pages), font_info) == []


# ─── All-caps fallback ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_caps_fallback_detects_short_uppercase_titles():
    pages = [(1, "APPROVAL PROCESS\nThe board reviews each submission.")]
    sections = detect_sections(_lines_of(pages))
    assert len(sections) == 1
    assert sections[0].matched_by == "caps"
    assert sections[0].title == "APPROVAL PROCESS"


@pytest.mark.unit
def test_caps_fallback_ignores_repeated_furniture():
    """First/last lines repeated on >= 2 pages are headers/footers."""
    pages = [
        (1, "CONFIDENTIAL INTERNAL USE\nBody content for page one goes here."),
        (2, "CONFIDENTIAL INTERNAL USE\nBody content for page two goes here."),
    ]
    assert detect_sections(_lines_of(pages)) == []


@pytest.mark.unit
def test_caps_fallback_ignores_solo_page_line():
    """A lone all-caps line as a page's entire content is a stamp."""
    pages = [(1, "RECEIVED"), (2, "Normal body text continues at length here.")]
    assert detect_sections(_lines_of(pages)) == []


@pytest.mark.unit
def test_caps_fallback_requires_two_to_eight_words():
    pages = [(1, "ONE\nThis line is far too long to be considered a heading title here")]
    assert detect_sections(_lines_of(pages)) == []


# ─── No-structure state ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_no_structure_produces_zero_sections():
    pages = [
        (
            1,
            "The quarterly facilities review found the north entrance carpet worn and scheduled replacement.",
        ),
        (
            2,
            "Roof inspection reported no ponding and recommended clearing the east drains.",
        ),
    ]
    sections = detect_sections(_lines_of(pages))
    assert sections == []  # explicitly supported, non-error state (Backend §20)


@pytest.mark.unit
def test_empty_input_produces_zero_sections():
    assert detect_sections([]) == []
    assert detect_sections([[]]) == []


# ─── PDF font extraction ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_pdf_font_info_reads_sizes_and_body_median():
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Big Heading Line", fontsize=24)
    page.insert_text((72, 120), "plain body text line", fontsize=12)
    page.insert_text((72, 140), "another plain body line", fontsize=12)
    data = doc.tobytes()
    doc.close()

    info = extract_pdf_font_info(data)
    assert 1 in info.lines
    heading = info.lines[1].get("Big Heading Line")
    assert heading is not None and heading.size > 20
    body = info.lines[1].get("plain body text line")
    assert body is not None and body.size < 14
    assert info.body_sizes[1] < 20  # median is the body size, not the heading


@pytest.mark.unit
def test_extract_pdf_font_info_garbage_bytes_never_raises():
    """Best-effort by contract: unopenable bytes → empty info, no raise."""
    info = extract_pdf_font_info(b"this is not a pdf at all")
    assert info.lines == {}
