"""
Structure-aware chunking (roadmap Phase 6 step 4; Backend §21).

Turns page/section-annotated text into retrieval units governed by these
rules, in priority order (Backend §21):

  1. Section boundaries are the preferred split points — a chunk NEVER
     spans two sections (never two top-level sections, not even two
     subsections); mid-section splits happen at paragraph boundaries.
  2. Target size ~500–800 tokens (configurable), hard maximum 1,000.
  3. Overlap ~10–15% between adjacent chunks WITHIN the same section only.
  4. Tables are never split mid-row — oversized tables split along
     row-group boundaries with a `forced_split` metadata note.
  5. Lists are kept intact where they fit (list integrity beats exact
     target size).
  6. Page boundaries never force a split — chunks may span pages
     (`end_page_id` in the schema).

Splitting never lands mid-sentence: oversized paragraphs split at sentence
boundaries; the degenerate giant-unbreakable case (a single sentence or
table row above the hard maximum) falls back to word/line boundaries with
an explicit metadata note (roadmap Phase 6 error handling).

Every chunk preserves: starting page (+ optional end page), section,
reading-order index, token count, and structural metadata (heading path,
table/list flags, forced-split notes) — the chunker's contract (Backend
§21; DB §16).

Pure module — token counting is injected, no I/O, fully unit-testable.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.ingestion.structure_detector import DetectedSection, LineInfo, _BULLET

logger = logging.getLogger(__name__)

# A table line (row/col-aware normalized text — DOCX parser emits "a | b | c").
_TABLE_ROW_SEPARATOR = " | "
_MIN_TABLE_SEPARATORS = 2

# Sentence boundary: terminal punctuation followed by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Conservative per-segment allowance for the "\n\n" join between segments,
# so packing decisions can never under-count a chunk's real token count.
_SEPARATOR_ALLOWANCE = 2


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Segment:
    """A block of same-kind content in reading order (chunker input)."""

    kind: str  # "heading" | "paragraph" | "table" | "list"
    text: str
    start_page: int
    end_page: int
    section_sort_order: int | None
    forced_split: bool = False
    tokens: int = 0


@dataclass
class ProposedChunk:
    """One chunk before persistence (chunker output)."""

    content: str
    token_count: int
    start_page: int
    end_page: int
    section_sort_order: int | None
    contains_table: bool = False
    contains_list: bool = False
    forced_split: bool = False
    chunk_index: int = 0  # assigned by chunk_documents (reading order)
    heading_path: list[str] = field(default_factory=list)
    section_number: str | None = None


@dataclass
class ChunkingPlan:
    """Complete chunker output for one document version (observability)."""

    chunks: list[ProposedChunk]
    table_pages: set[int] = field(default_factory=set)


# ── Segmentation: pages → kind-annotated blocks ───────────────────────────────

def build_segments(
    pages_lines: Sequence[list[LineInfo]],
    sections: Sequence[DetectedSection],
) -> list[Segment]:
    """Assemble kind-annotated segments in reading order.

    - Heading lines become "heading" segments attributed to their own
      section (the title text leads its section's first chunk — good for
      retrieval and for citation context).
    - Contiguous table-row lines (row/col-aware "a | b | c") merge into one
      "table" segment.
    - Contiguous list items merge into one "list" segment (kept intact where
      they fit — Backend §21 rule 5).
    - Remaining consecutive lines join into "paragraph" segments (page ends
      are paragraph boundaries; chunks may still span pages).
    """
    flat: list[LineInfo] = [line for page in pages_lines for line in page]

    # Section lookup by heading line position (sections are in reading order)
    heading_at: dict[int, DetectedSection] = {
        s.line_index: s for s in sections if s.line_index >= 0
    }
    ordered_heading_indexes = sorted(heading_at)

    segments: list[Segment] = []
    current_section: int | None = None
    buffer_lines: list[LineInfo] = []
    buffer_kind: str = "paragraph"

    def flush_buffer() -> None:
        nonlocal buffer_lines, buffer_kind
        if not buffer_lines:
            return
        text = (
            buffer_lines[0].text
            if len(buffer_lines) == 1
            else " ".join(line.text for line in buffer_lines)
        )
        if buffer_kind == "table":
            text = "\n".join(line.text for line in buffer_lines)
        segments.append(
            Segment(
                kind=buffer_kind,
                text=text,
                start_page=buffer_lines[0].page_number,
                end_page=buffer_lines[-1].page_number,
                section_sort_order=current_section,
            )
        )
        buffer_lines = []

    heading_pointer = 0
    for index, line in enumerate(flat):
        # Advance the section pointer: sections whose heading line we passed
        while (
            heading_pointer < len(ordered_heading_indexes)
            and ordered_heading_indexes[heading_pointer] <= index
        ):
            current_section = heading_at[
                ordered_heading_indexes[heading_pointer]
            ].sort_order
            heading_pointer += 1

        if index in heading_at:
            flush_buffer()
            buffer_kind = "heading"
            buffer_lines = [line]
            flush_buffer()
            buffer_kind = "paragraph"
            continue

        if _is_table_line(line.text):
            if buffer_kind != "table":
                flush_buffer()
                buffer_kind = "table"
            buffer_lines.append(line)
            continue

        if _BULLET.match(line.text):
            if buffer_kind != "list":
                flush_buffer()
                buffer_kind = "list"
            buffer_lines.append(line)
            continue

        if buffer_kind != "paragraph":
            flush_buffer()
            buffer_kind = "paragraph"
        buffer_lines.append(line)

    flush_buffer()
    return segments


def _is_table_line(text: str) -> bool:
    """Row/col-aware table line (≥ 2 cell separators)."""
    return text.count(_TABLE_ROW_SEPARATOR) >= _MIN_TABLE_SEPARATORS


# ── Oversized-segment splitting (never mid-sentence / mid-row / mid-item) ─────

def _split_segment(
    segment: Segment,
    *,
    piece_limit: int,
    count: Callable[[str], int],
) -> list[Segment]:
    """Split one oversized segment into pieces ≤ piece_limit.

    paragraph/heading → sentences; table → rows; list → items. The
    degenerate single-unbreakable-unit case falls back to word boundaries
    with the forced_split flag set (roadmap Phase 6 error handling).
    """
    if segment.kind == "table":
        units = segment.text.split("\n")
    elif segment.kind == "list":
        units = segment.text.split("\n")
    else:
        units = _SENTENCE_SPLIT.split(segment.text)

    pieces: list[Segment] = []
    current: list[str] = []
    current_tokens = 0

    def flush_units() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        # Tables/lists keep line (row/item) boundaries; prose joins as text
        separator = "\n" if segment.kind in ("table", "list") else " "
        text = separator.join(unit for unit in current if unit)
        pieces.append(
            Segment(
                kind=segment.kind,
                text=text,
                start_page=segment.start_page,
                end_page=segment.end_page,
                section_sort_order=segment.section_sort_order,
                forced_split=True,
                tokens=count(text),
            )
        )
        current = []
        current_tokens = 0

    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        unit_tokens = count(unit)
        if unit_tokens > piece_limit:
            # Degenerate: one unbreakable unit — split at word boundaries.
            if current:
                flush_units()
            for words_text in _split_words(unit, piece_limit, count):
                pieces.append(
                    Segment(
                        kind=segment.kind,
                        text=words_text,
                        start_page=segment.start_page,
                        end_page=segment.end_page,
                        section_sort_order=segment.section_sort_order,
                        forced_split=True,
                        tokens=count(words_text),
                    )
                )
            continue
        if current and current_tokens + unit_tokens > piece_limit:
            flush_units()
        current.append(unit)
        current_tokens += unit_tokens

    flush_units()
    return pieces


def _split_words(
    text: str, piece_limit: int, count: Callable[[str], int]
) -> list[str]:
    """Last-resort word-boundary splitting (never mid-word)."""
    words = text.split()
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        word_tokens = count(word)
        if current and current_tokens + word_tokens > piece_limit:
            pieces.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(word)
        current_tokens += word_tokens
    if current:
        pieces.append(" ".join(current))
    return pieces


def _overlap_tail(
    flushed: list[tuple[Segment, bool]],
    *,
    budget: int,
    count: Callable[[str], int],
) -> tuple[list[Segment], int]:
    """Build the overlap tail carried into the next chunk (same section).

    Trailing PARAGRAPH segments of the flushed chunk only — tables/lists
    don't make meaningful context tails (Backend §21 rule 3: overlap is
    about a sentence severed at a split point). When a whole segment
    doesn't fit the budget, the segment's trailing SENTENCES are taken
    instead — overlap stays within budget and never cuts mid-sentence.
    Returns (tail segments in reading order, their token count).
    """
    tail: list[Segment] = []
    used = 0
    for seg, is_overlap in reversed(flushed):
        if is_overlap or seg.kind != "paragraph":
            continue
        if seg.tokens + used <= budget:
            tail.insert(0, seg)
            used += seg.tokens
            continue
        # The segment exceeds the remaining budget — take its trailing
        # sentences (whole sentences only) that fit
        taken: list[str] = []
        taken_tokens = 0
        for sentence in reversed(_SENTENCE_SPLIT.split(seg.text)):
            sentence = sentence.strip()
            if not sentence:
                continue
            sentence_tokens = count(sentence)
            if taken_tokens + sentence_tokens > budget:
                break
            taken.insert(0, sentence)
            taken_tokens += sentence_tokens
        if taken:
            text = " ".join(taken)
            tail.insert(
                0,
                Segment(
                    kind="paragraph",
                    text=text,
                    start_page=seg.start_page,
                    end_page=seg.end_page,
                    section_sort_order=seg.section_sort_order,
                    tokens=taken_tokens,
                ),
            )
            used += taken_tokens
        break
    return tail, used


# ── Chunk assembly (the rule priority) ────────────────────────────────────────

def chunk_documents_segments(
    segments: Sequence[Segment],
    sections: Sequence[DetectedSection],
    *,
    count: Callable[[str], int],
    target_min_tokens: int,
    target_max_tokens: int,
    hard_max_tokens: int,
    overlap_tokens: int,
    table_pages: Sequence[int] = (),
) -> ChunkingPlan:
    """Pack segments into chunks under the Backend §21 rule priority.

    Section boundaries are hard split points; within a section, chunks
    accumulate to the target band, carry an overlap tail into the next
    chunk, and never exceed the hard maximum. Page attribution follows the
    segments, so chunks spanning pages just work.
    """
    if hard_max_tokens < target_max_tokens:  # defensive — misconfiguration
        hard_max_tokens = target_max_tokens

    sections_by_order = {s.sort_order: s for s in sections}
    table_page_set = set(table_pages)
    plan_segments = list(segments)

    # Pre-split any single segment above the hard maximum
    expanded: list[Segment] = []
    for segment in plan_segments:
        if segment.tokens > hard_max_tokens:
            expanded.extend(
                _split_segment(segment, piece_limit=hard_max_tokens, count=count)
            )
        else:
            expanded.append(segment)

    chunks: list[ProposedChunk] = []

    # Buffers: pending pieces as (segment, is_overlap). real_in_buffer
    # includes a small per-segment separator allowance so the final
    # count(content) can never push a chunk past the hard maximum.
    pending: list[tuple[Segment, bool]] = []
    pending_section: int | None = None
    overlap_in_buffer = 0
    real_in_buffer = 0

    def flush() -> None:
        nonlocal pending, overlap_in_buffer, real_in_buffer
        if not pending:
            return
        content = "\n\n".join(seg.text for seg, _ in pending)
        chunk = ProposedChunk(
            content=content,
            token_count=count(content),
            start_page=pending[0][0].start_page,
            end_page=pending[-1][0].end_page,
            section_sort_order=pending_section,
            contains_table=any(seg.kind == "table" for seg, _ in pending),
            contains_list=any(seg.kind == "list" for seg, _ in pending),
            forced_split=any(seg.forced_split for seg, _ in pending),
        )
        if chunk.section_sort_order in sections_by_order:
            section = sections_by_order[chunk.section_sort_order]
            chunk.heading_path = section.heading_path(list(sections))
            chunk.section_number = section.section_number
        chunks.append(chunk)
        pending = []
        overlap_in_buffer = 0
        real_in_buffer = 0

    for segment in expanded:
        # Rule 1: section boundaries are hard split points — never overlap
        # across a section (Backend §21 rule 3)
        if pending and segment.section_sort_order != pending_section:
            flush()

        # Rule 2/3: flush at the target band, carrying an overlap tail
        if (
            pending
            and real_in_buffer + segment.tokens + overlap_in_buffer > target_max_tokens
        ):
            flushed = pending
            flush()
            tail, used = _overlap_tail(flushed, budget=min(overlap_tokens, hard_max_tokens // 2), count=count)
            # Drop the overlap when even the first incoming piece couldn't
            # fit with it (hard-max invariant)
            if used + segment.tokens > hard_max_tokens:
                tail, used = [], 0
            pending = [(seg, True) for seg in tail]
            pending_section = segment.section_sort_order
            overlap_in_buffer = used
            real_in_buffer = 0

        if not pending:
            pending_section = segment.section_sort_order

        pending.append((segment, False))
        real_in_buffer += segment.tokens + _SEPARATOR_ALLOWANCE

    flush()

    # target_min gives small same-section chunks a second chance: merge a
    # trailing undersized chunk into its same-section neighbor when the
    # combined result still fits the target band (small sections
    # legitimately stay small — never merged across sections).
    if target_min_tokens > 0 and len(chunks) > 1:
        chunks = _merge_undersized(
            chunks,
            count=count,
            target_min_tokens=target_min_tokens,
            target_max_tokens=target_max_tokens,
        )

    # Page-level table signal: a chunk whose page range crosses a
    # find_tables-detected table carries contains_table even when no
    # pipe-row segment did
    if table_page_set:
        for chunk in chunks:
            if not chunk.contains_table and table_page_set.intersection(
                range(chunk.start_page, chunk.end_page + 1)
            ):
                chunk.contains_table = True

    for index, chunk in enumerate(chunks):
        chunk.chunk_index = index
    return ChunkingPlan(chunks=chunks, table_pages=table_page_set)


def _merge_undersized(
    chunks: list[ProposedChunk],
    *,
    count: Callable[[str], int],
    target_min_tokens: int,
    target_max_tokens: int,
) -> list[ProposedChunk]:
    """Merge undersized chunks into the following same-section chunk."""
    merged: list[ProposedChunk] = []
    for chunk in chunks:
        if merged:
            previous = merged[-1]
            if (
                previous.token_count < target_min_tokens
                and previous.section_sort_order == chunk.section_sort_order
                and previous.token_count + chunk.token_count <= target_max_tokens
            ):
                previous.content = f"{previous.content}\n\n{chunk.content}"
                previous.token_count = count(previous.content)
                previous.end_page = max(previous.end_page, chunk.end_page)
                previous.contains_table = (
                    previous.contains_table or chunk.contains_table
                )
                previous.contains_list = previous.contains_list or chunk.contains_list
                previous.forced_split = previous.forced_split or chunk.forced_split
                continue
        merged.append(chunk)
    return merged


def chunk_document(
    pages_lines: Sequence[list[LineInfo]],
    sections: Sequence[DetectedSection],
    *,
    count: Callable[[str], int],
    target_min_tokens: int,
    target_max_tokens: int,
    hard_max_tokens: int,
    overlap_tokens: int,
    table_pages: Sequence[int] = (),
) -> ChunkingPlan:
    """One-call entry: page lines + sections → chunks (pure)."""
    segments = build_segments(pages_lines, sections)
    for segment in segments:
        segment.tokens = count(segment.text)
    return chunk_documents_segments(
        segments,
        sections,
        count=count,
        target_min_tokens=target_min_tokens,
        target_max_tokens=target_max_tokens,
        hard_max_tokens=hard_max_tokens,
        overlap_tokens=overlap_tokens,
        table_pages=table_pages,
    )
