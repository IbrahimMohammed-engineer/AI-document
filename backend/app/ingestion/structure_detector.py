"""
Heuristic document structure detection (roadmap Phase 6 step 1; Backend §20).

Turns extracted pages into a hierarchical section tree by combining:

  1. Numbering-pattern matching — `4.`, `4.2`, `Section 4`, `IV.`,
     `Appendix A` — with the heading level derived from numbering depth.
  2. Font/typography signals from PyMuPDF's rich extraction (font size vs
     the page's median body size, bold weight) — for PDFs, per-line font
     metadata is matched onto the STORED page text by normalized lookup.
  3. A conservative all-caps fallback for text without any font signal
     (OCR pages, DOCX) — deliberately strict to avoid junk TOC entries.

This is deliberately NOT an LLM call: structure detection runs on every
page of every document, and heuristics handle the great majority of
structured business documents well (Backend §20).

The heading-level stack algorithm (Backend §20): a new heading at level N
becomes a child of the most recent still-open heading at level N−1 —
implemented as pop-while(top.level >= N) then parent = stack top.

Documents with NO detectable structure produce ZERO sections — an
explicitly supported, non-error state (Backend §20; FE §6.5 "No structure
detected"); chunking degrades to page/paragraph-aware splitting.

Tables/lists are NOT separate entities: `detect_tables` marks where tables
appear so the chunker can set `contains_table` metadata and never split a
row across chunks (Backend §20).

Pure module — no I/O, fully unit-testable (mirrors ingestion/parser.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────

# A heading line is short; longer decimal-numbered lines are list items.
_MAX_HEADING_WORDS = 24
# A single-level decimal ("1. Foo") is only a heading when short, unless a
# font signal backs it up — list items share the "N. " shape.
_MAX_SINGLE_LEVEL_WORDS = 8
_MAX_FONT_HEADING_CHARS = 100
_MAX_CAPS_HEADING_CHARS = 60
# Font thresholds relative to the page's median body size (Backend §20).
_FONT_HEADING_RATIO = 1.15
_FONT_LEVEL1_RATIO = 1.4
_FONT_LEVEL2_RATIO = 1.2
# Size bounds on the auxiliary extraction output (metadata stays bounded).
_MAX_RECORDED_TABLES = 200
# Header/footer suppression: first/last page lines repeated on >= 2 pages.
_MIN_REPEATS_FOR_FURNITURE = 2


# ── Numbering patterns ────────────────────────────────────────────────────────

# Multi-level decimal: "4.2 Title", "4.2.1 Title", "3.10) Title"
_DECIMAL_MULTI = re.compile(
    r"^(?P<num>\d+(?:\.\d+)+)[.)]?\s+(?P<title>\S.*)$"
)
# Single-level decimal: "4. Title" / "4) Title" — ambiguous with list items,
# only promoted to a heading with short titles or a font signal.
_DECIMAL_SINGLE = re.compile(r"^(?P<num>\d+)[.)]\s+(?P<title>\S.*)$")
# "Section 4 …" / "SECTION 4: …"
_SECTION_WORD = re.compile(
    r"^Section\s+(?P<num>\d+)\s*[:.\-–—]?\s*(?P<title>\S?.*)$", re.IGNORECASE
)
# Roman numerals: "IV. Scope"
_ROMAN = re.compile(r"^(?P<num>[IVXLCDM]{1,7})[.)]\s+(?P<title>\S.*)$")
# "Appendix A …" / "Appendix A.2 …"
_APPENDIX = re.compile(
    r"^Appendix\s+(?P<num>[A-Z](?:\.\d+)*)\s*[:.\-–—]?\s*(?P<title>\S?.*)$",
    re.IGNORECASE,
)

# Bullet/list-item shapes (recognized so they are NOT mistaken for headings
# and so the chunker can keep list items together — Backend §21 rule 5).
_BULLET = re.compile(r"^(?:[-•*‣◦▪]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+")

_SENTENCE_END = re.compile(r"[.!?:;]\s*$")


def normalize_line(text: str) -> str:
    """Collapse whitespace — the key for font-lookup line matching."""
    return " ".join(text.split())


def _word_count(text: str) -> int:
    return len(text.split())


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class LineFont:
    """Font signals for one physical line (PDF-native pages only)."""

    size: float
    bold: bool


@dataclass
class PdfFontInfo:
    """Per-page font signals extracted from the original PDF bytes.

    `lines` maps page_number → {normalized_line_text → font info}; lookup
    misses (OCR pages, ligature drift) simply mean no signal — pattern
    detection still applies.
    """

    lines: dict[int, dict[str, LineFont]] = field(default_factory=dict)
    body_sizes: dict[int, float] = field(default_factory=dict)
    # page_number → [{"bbox": [x0, y0, x1, y1]}] (size-bounded)
    tables: dict[int, list[dict]] = field(default_factory=dict)

    def lookup(self, page_number: int, text: str) -> LineFont | None:
        return self.lines.get(page_number, {}).get(normalize_line(text))

    def body_size(self, page_number: int) -> float | None:
        return self.body_sizes.get(page_number)


@dataclass
class LineInfo:
    """One content line with optional font signal (stored text is canonical)."""

    text: str
    page_number: int
    font: LineFont | None = None


@dataclass
class DetectedSection:
    """One document_sections row before persistence (parent by sort_order)."""

    title: str
    section_number: str | None
    level: int
    start_page: int
    end_page: int | None
    sort_order: int
    parent_sort_order: int | None = None
    matched_by: str = "numbering"  # numbering | font | caps (tests/debug)
    # Index of the heading line within the flat reading-order line list —
    # lets the chunker attribute content between headings to sections.
    line_index: int = -1

    def heading_path(self, all_sections: list[DetectedSection]) -> list[str]:
        """Root→self titles (breadcrumb for chunk metadata)."""
        by_order = {s.sort_order: s for s in all_sections}
        path: list[str] = []
        current: DetectedSection | None = self
        while current is not None:
            path.append(current.title)
            current = (
                by_order.get(current.parent_sort_order)
                if current.parent_sort_order is not None
                else None
            )
        path.reverse()
        return path


# ── PDF font-signal extraction ────────────────────────────────────────────────

def extract_pdf_font_info(data: bytes) -> PdfFontInfo:
    """Extract per-line font signals + table bounding boxes from PDF bytes.

    Best-effort by contract: structural surprises raise nothing — callers
    treat a None/empty result as "no font signals" and detection degrades to
    numbering patterns alone (graceful degradation, Backend §20).
    """
    info = PdfFontInfo()
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # pragma: no cover — defensive; bytes verified upstream
        logger.warning("Font-signal extraction could not open the PDF: %s", exc)
        return info

    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            page_number = index + 1
            page_lines: dict[str, LineFont] = {}
            sizes: list[float] = []

            raw = page.get_text("dict")
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    text = normalize_line(
                        "".join(span.get("text", "") for span in spans)
                    )
                    if not text:
                        continue
                    max_size = max(
                        (float(span.get("size", 0.0)) for span in spans),
                        default=0.0,
                    )
                    bold = any(
                        "bold" in str(span.get("font", "")).lower()
                        for span in spans
                    )
                    sizes.extend(
                        float(span.get("size", 0.0))
                        for span in spans
                        if span.get("text", "").strip()
                    )
                    # Keep the LARGEST signal seen for duplicated line text.
                    existing = page_lines.get(text)
                    if existing is None or max_size > existing.size:
                        page_lines[text] = LineFont(size=max_size, bold=bold)

            info.lines[page_number] = page_lines
            if sizes:
                ordered = sorted(sizes)
                info.body_sizes[page_number] = ordered[len(ordered) // 2]

            # Table bounding boxes — best-effort, bounded (metadata stays small)
            if len(info.tables) < _MAX_RECORDED_TABLES:
                try:
                    found = page.find_tables()
                    boxes = [
                        {"bbox": [round(v, 2) for v in table.bbox]}
                        for table in (found.tables or [])
                    ]
                    if boxes:
                        info.tables[page_number] = boxes
                except Exception:  # noqa: S110 — tables are optional signal
                    pass
    except Exception as exc:
        logger.warning("Font-signal extraction failed mid-document: %s", exc)
    finally:
        try:
            doc.close()
        except Exception:  # pragma: no cover — close is best-effort
            pass
    return info


# ── Page-lines assembly (stored text is the content source of truth) ─────────

def build_page_lines(
    page_number: int,
    text: str,
    font_info: PdfFontInfo | None,
) -> list[LineInfo]:
    """Split one stored page's text into lines, enriching with font signals."""
    page_fonts = font_info.lines.get(page_number, {}) if font_info else {}
    lines: list[LineInfo] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        lines.append(
            LineInfo(
                text=stripped,
                page_number=page_number,
                font=page_fonts.get(normalize_line(stripped)),
            )
        )
    return lines


def _find_repeated_furniture(pages_lines: list[list[LineInfo]]) -> set[str]:
    """First/last page lines repeated across >= 2 pages are headers/footers.

    They stay in chunk CONTENT (the stored text is never altered) but are
    excluded from heading candidates so running furniture doesn't pollute
    the TOC.
    """
    counts: dict[str, set[int]] = {}
    for lines in pages_lines:
        if not lines:
            continue
        for edge in (lines[0], lines[-1]):
            counts.setdefault(normalize_line(edge.text), set()).add(
                edge.page_number
            )
    return {
        text
        for text, page_set in counts.items()
        if len(page_set) >= _MIN_REPEATS_FOR_FURNITURE
    }


def _find_solo_lines(pages_lines: list[list[LineInfo]]) -> set[str]:
    """Lines that are a page's ENTIRE content — stamps/scan artifacts.

    A lone all-caps line ("RECEIVED", an OCR stamp) is not a section
    heading; the caps fallback skips these.
    """
    return {
        normalize_line(lines[0].text)
        for lines in pages_lines
        if len(lines) == 1
    }


# ── Heading classification ────────────────────────────────────────────────────

def _match_numbering(
    line: LineInfo,
) -> tuple[str, int, str] | None:
    """Match numbering patterns → (section_number, level, title) or None.

    Ambiguity guard: single-level decimals ("1. Install the appliance…")
    are list items when long — unless the line's font shouts "heading".
    """
    text = line.text
    words = _word_count(text)

    match = _DECIMAL_MULTI.match(text)
    if match:
        if words <= _MAX_HEADING_WORDS or (line.font and line.font.bold):
            num = match.group("num")
            return num, num.count(".") + 1, match.group("title").strip()
        return None

    match = _SECTION_WORD.match(text)
    if match:
        title = match.group("title").strip()
        if title and words <= _MAX_HEADING_WORDS:
            return match.group("num"), 1, title
        return None

    match = _APPENDIX.match(text)
    if match:
        title = match.group("title").strip()
        num = match.group("num")
        if title and words <= _MAX_HEADING_WORDS:
            return f"Appendix {num}", num.count(".") + 1, title
        return None

    match = _ROMAN.match(text)
    if match:
        # Word-count guard only — bold-backed Roman headings were too
        # strict for font-less text (OCR/DOCX), and Roman list items are
        # rare in business documents.
        title = match.group("title").strip()
        if title and words <= _MAX_HEADING_WORDS:
            return match.group("num"), 1, title
        return None

    match = _DECIMAL_SINGLE.match(text)
    if match:
        font_backed = bool(line.font and line.font.bold)
        if words <= _MAX_SINGLE_LEVEL_WORDS or font_backed:
            return match.group("num"), 1, match.group("title").strip()
        return None

    return None


def _match_font_heading(
    line: LineInfo, body_size: float | None
) -> int | None:
    """Font-signal heading (no numbering) → level, or None.

    Conservative: short lines only, weighted/bigger than the page's body
    text, and not sentence-shaped.
    """
    if line.font is None or body_size is None:
        return None
    text = line.text
    if len(text) > _MAX_FONT_HEADING_CHARS or _word_count(text) > 14:
        return None
    if _SENTENCE_END.search(text):
        return None
    font = line.font
    if font.size >= body_size * _FONT_LEVEL1_RATIO:
        return 1
    if font.size >= body_size * _FONT_LEVEL2_RATIO:
        return 2
    if font.bold and font.size >= body_size * _FONT_HEADING_RATIO:
        return 3
    return None


def _match_caps_heading(line: LineInfo) -> bool:
    """Strict all-caps fallback for text with no font signal (OCR/DOCX)."""
    text = line.text
    if len(text) > _MAX_CAPS_HEADING_CHARS:
        return False
    if _word_count(text) < 2 or _word_count(text) > 8:
        return False
    if text != text.upper():
        return False
    if _SENTENCE_END.search(text) or text.endswith(","):
        return False
    # Require at least one letter (pure numbers/symbols aren't titles)
    return any(ch.isalpha() for ch in text)


# ── Section-tree assembly (the heading-level stack algorithm) ─────────────────

def detect_sections(
    pages_lines: Sequence[LineInfo | list[LineInfo]],
    font_info: PdfFontInfo | None = None,
) -> list[DetectedSection]:
    """Detect the section tree from page lines (Backend §20).

    Accepts a flat sequence of LineInfo (reading order) or per-page lists.
    `font_info` supplies the page-level median body sizes used by the
    font-signal fallback. Returns sections with parent links
    (parent_sort_order) and end_page derived from where the next
    same-or-higher-level heading begins.
    """
    flat: list[LineInfo] = []
    per_page: list[list[LineInfo]] = []
    for entry in pages_lines:
        if isinstance(entry, list):
            flat.extend(entry)
            per_page.append(entry)
        else:
            flat.append(entry)
    if not flat:
        return []

    furniture = _find_repeated_furniture(per_page)
    solo_lines = _find_solo_lines(per_page)

    # ── Classify heading candidates in reading order ──────────────────────
    # (flat_index, line, level, title, section_number, matched_by)
    candidates: list[tuple[int, LineInfo, int, str, str | None, str]] = []
    saw_numbering = False

    for index, line in enumerate(flat):
        normalized = normalize_line(line.text)
        if normalized in furniture:
            continue
        numbered = _match_numbering(line)
        if numbered is not None:
            section_number, level, title = numbered
            if title:
                saw_numbering = True
                candidates.append(
                    (index, line, level, title, section_number, "numbering")
                )
            continue
        # Font/caps fallbacks are skipped once the document demonstrably uses
        # numbered headings — mixing signals there mostly yields noise.
        if saw_numbering:
            continue
        body_size = font_info.body_size(line.page_number) if font_info else None
        font_level = _match_font_heading(line, body_size)
        if font_level is not None:
            candidates.append((index, line, font_level, line.text, None, "font"))
            continue
        if (
            line.font is None
            and normalized not in solo_lines
            and _match_caps_heading(line)
        ):
            candidates.append((index, line, 2, line.text, None, "caps"))

    if not candidates:
        return []

    last_page = flat[-1].page_number

    # ── Heading-level stack → parent links (Backend §20) ──────────────────
    sections: list[DetectedSection] = []
    stack: list[DetectedSection] = []
    for index, line, heading_level, heading_title, heading_number, matched_by in candidates:
        while stack and stack[-1].level >= heading_level:
            stack.pop()
        parent = stack[-1] if stack else None
        section = DetectedSection(
            title=heading_title,
            section_number=heading_number,
            level=heading_level,
            start_page=line.page_number,
            end_page=None,
            sort_order=len(sections),
            parent_sort_order=parent.sort_order if parent else None,
            matched_by=matched_by,
            line_index=index,
        )
        sections.append(section)
        stack.append(section)

    # ── end_page: where the next same-or-higher-level heading begins ──────
    for i, section in enumerate(sections):
        section.end_page = last_page
        for later in sections[i + 1:]:
            if later.level <= section.level:
                section.end_page = later.start_page
                break
    return sections
