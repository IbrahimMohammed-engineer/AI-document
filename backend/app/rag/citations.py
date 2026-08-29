"""
Citation extraction and resolution (Phase 10).

Backend §35: after generation completes, the answer text is pattern-matched
for inline references (``[1]``, ``[2]``, ``[1][2]``) and each reference is
resolved to a real chunk through the backend-owned ``SOURCE N → chunk_id``
map built during context assembly (``ContextBundle.source_index``).

THE ARCHITECTURAL LINCHPIN (Backend §35): citation resolution NEVER depends
on the LLM correctly reproducing a document name or page number — the LLM
only echoes SOURCE labels; every citation field (document, version, page,
section) is looked up from the backend's own record of what it put in the
prompt.  Citation data is therefore trustworthy BY CONSTRUCTION.

quoted_text extraction (Backend §35): the quoted span always comes from the
ACTUAL chunk content — never from model output.  V1 policy:
  - chunk at or under ``citation_quoted_span_max_chars`` → quoted in full
    (chunks are already sized to a single retrieval-relevant unit);
  - longer chunks → the sentence with the highest lexical overlap with the
    claim text, with exact ``char_start``/``char_end`` offsets into the chunk
    content — driving the frontend's highlight overlay.

Invalid references (``[3]`` with 2 sources) are stripped from the text and
reported as a generation-quality signal — never persisted (Backend §36).

This module is PURE (no I/O, no LLM) — every function is unit-testable in
isolation.

See:
  Backend-Architecture-Documentation.md §35 (Citation Generation)
  Backend-Architecture-Documentation.md §33 (Context Assembly — the map)
  roadmap Phase 10 steps 1–2, 8
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.rag.context_builder import ContextBundle

logger = logging.getLogger(__name__)


# ── Reference pattern ─────────────────────────────────────────────────────────
#
# Matches [1], [23], [1][2], [SOURCE 1] — the citation format the system
# prompt (v1) instructs the model to emit.  Word boundaries prevent [1] from
# matching inside "[10]" or "arr[1]"-style artifacts; adjacent markers
# ([1][2]) each match independently (roadmap Phase 10 step 8 — multiple
# citations per claim remain independent, never merged).

_CITATION_RE = re.compile(r"\[(?:SOURCE\s*)?(\d{1,2})\]")


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class QuotedSpan:
    """An exact span of REAL source text within a chunk's content.

    ``char_start``/``char_end`` are offsets into the chunk content and drive
    the frontend's highlight overlay.  ``context_before``/``after`` carry the
    surrounding chunk text for the SourcePreview popover (FE §12).
    """

    text: str
    char_start: int
    char_end: int
    context_before: str = ""
    context_after: str = ""


@dataclass
class ReferenceMatch:
    """One inline reference found in the answer text."""

    index: int            # the N in [N]
    start: int            # char offset of the marker in the answer
    end: int              # end offset (exclusive)


@dataclass
class ResolvedCitation:
    """A fully resolved citation — everything persistence and the FE need.

    Every field except ``quoted``/``claim_text`` round-trips unchanged from
    the ``SourceBlock`` that produced the SOURCE N block — metadata
    preservation from context assembly, never re-derived after generation
    (Backend §33/§35).
    """

    index: int
    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str
    page_number: int
    section: str | None
    relevance: float
    quoted: QuotedSpan
    claim_text: str = ""        # the sentence(s) this citation supports


@dataclass
class CitationExtraction:
    """Outcome of extracting citations from one answer."""

    citations: list[ResolvedCitation] = field(default_factory=list)
    # Answer text with invalid references removed (valid markers intact).
    cleaned_text: str = ""
    # Invalid reference indices (e.g. [3] with 2 sources) — a generation-
    # quality signal logged for Phase 18, never persisted (Backend §36).
    invalid_indices: list[int] = field(default_factory=list)


# ── Reference extraction (pure) ───────────────────────────────────────────────

def extract_references(answer_text: str) -> list[ReferenceMatch]:
    """Find every inline citation marker in the answer, in reading order.

    Repeated references to the same source are kept — each occurrence is an
    independent claim→source link in the text.
    """
    return [
        ReferenceMatch(index=int(m.group(1)), start=m.start(), end=m.end())
        for m in _CITATION_RE.finditer(answer_text)
    ]


def strip_unresolvable_references(
    answer_text: str,
    valid_indices: set[int],
) -> tuple[str, list[int]]:
    """Remove markers whose index does not resolve to a real source.

    Returns the cleaned text and the sorted list of invalid indices that were
    removed.  Valid markers (and all non-marker text) pass through untouched.
    """
    invalid: set[int] = set()

    def _replace(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if idx in valid_indices:
            return m.group(0)
        invalid.add(idx)
        return ""

    cleaned = _CITATION_RE.sub(_replace, answer_text)
    # Collapse doubled spaces left behind by removed markers.
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned, sorted(invalid)


# ── Quoted-span computation (pure — Backend §35) ──────────────────────────────

_SENTENCE_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])"
    # A citation marker directly follows the sentence it annotates — the
    # boundary after "] Next" is a claim boundary too, so each marker
    # attaches to its own claim's sentence.
    r"|(?<=\])\s+"
)


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """Segment text into sentences, returning (sentence, start, end) offsets.

    A lightweight regex splitter — sentence-level granularity is the
    documented V1 approach (Backend §36; full claim decomposition is a future
    refinement).  Offsets index into the original text.
    """
    sentences: list[tuple[str, int, int]] = []
    start = 0
    for m in _SENTENCE_RE.finditer(text):
        end = m.start()
        piece = text[start:end]
        if piece.strip():
            # Trim leading whitespace but keep true offsets
            lead = len(piece) - len(piece.lstrip())
            sentences.append((piece.strip(), start + lead, end))
        start = m.end()
    tail = text[start:]
    if tail.strip():
        lead = len(tail) - len(tail.lstrip())
        sentences.append((tail.strip(), start + lead, len(text)))
    return sentences


def _token_set(text: str) -> set[str]:
    """Lowercased word set for lexical-overlap scoring (stopword-light)."""
    return {
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 2  # skip tiny tokens (the, of, a …)
    }


def find_quoted_span(
    chunk_content: str,
    claim_text: str,
    *,
    max_full_quote_chars: int | None = None,
) -> QuotedSpan:
    """Pick the most relevant span of the ACTUAL chunk content (Backend §35).

    Policy (roadmap Phase 10 step 2):
      - chunk short enough → the full content is the quote (chunks are already
        single-topic retrieval units; nothing better to pick);
      - otherwise → the sentence with the highest lexical-token overlap with
        the claim; exact offsets into the chunk content are computed.
    """
    settings = get_settings()
    if max_full_quote_chars is None:
        max_full_quote_chars = settings.citation_quoted_span_max_chars

    content = chunk_content or ""
    if len(content) <= max_full_quote_chars:
        return QuotedSpan(
            text=content,
            char_start=0,
            char_end=len(content),
            context_before="",
            context_after="",
        )

    claim_tokens = _token_set(claim_text)
    best: tuple[str, int, int] | None = None
    best_score = -1.0
    for sentence, start, end in split_sentences(content):
        tokens = _token_set(sentence)
        if not tokens:
            continue
        overlap = len(claim_tokens & tokens) / len(tokens)
        # Slight length normalization: prefer the sentence that covers more
        # of the claim too (claim coverage), so a short generic sentence
        # doesn't beat a substantive one on equal overlap.
        claim_coverage = (
            len(claim_tokens & tokens) / len(claim_tokens) if claim_tokens else 0.0
        )
        score = overlap + claim_coverage
        if score > best_score:
            best_score = score
            best = (sentence, start, end)

    if best is not None:
        sentence, start, end = best
        whole_content = start == 0 and end >= len(content)
        if not (whole_content and len(content) > max_full_quote_chars):
            # A real sub-span was picked (the split_sentences tail always
            # yields one candidate; when it is just the whole unsplit chunk
            # we prefer the bounded head span below instead).
            return QuotedSpan(
                text=sentence,
                char_start=start,
                char_end=end,
                context_before=content[max(0, start - 120):start].lstrip(),
                context_after=content[end:end + 120].rstrip(),
            )

    # No sentence structure (or only the whole unsplit chunk) — fall back to
    # a bounded head span so quoted_text never balloons.
    end = min(len(content), max_full_quote_chars)
    return QuotedSpan(
        text=content[:end], char_start=0, char_end=end,
        context_after=content[end:],
    )


# ── Resolution (pure — maps references through the backend-owned context) ────

def resolve_citations(
    answer_text: str,
    bundle: ContextBundle,
    *,
    quoted_span_max_chars: int | None = None,
) -> CitationExtraction:
    """Extract, resolve and verify every inline reference in the answer.

    Resolution walks the backend-owned ``SOURCE N → chunk`` map from context
    assembly — never model memory (Backend §35).  References that resolve
    produce :class:`ResolvedCitation` objects with real quoted spans from the
    actual chunk content; references that do not resolve are stripped from
    the returned cleaned text and reported as invalid indices.

    Multiple references to the same source produce independent citation
    objects (``[1] … [1]`` = two rows) — never merged (roadmap Phase 10
    step 8).
    """
    valid_indices = set(bundle.source_index.keys())
    cleaned_text, invalid_indices = strip_unresolvable_references(
        answer_text, valid_indices
    )
    if invalid_indices:
        logger.warning(
            "Citation extraction: stripped invalid reference(s) %s — only %d "
            "source(s) were provided (generation-quality signal)",
            invalid_indices, len(valid_indices),
        )

    # The claim a citation supports: the sentence containing the marker
    # (sentence-level granularity — Backend §36 V1).  Compute once.
    sentences = split_sentences(cleaned_text)

    def _claim_for(marker_start: int) -> str:
        for text, start, end in sentences:
            if start <= marker_start <= end:
                return text
        return ""

    citations: list[ResolvedCitation] = []
    for match in extract_references(cleaned_text):
        block = next(
            (b for b in bundle.blocks if b.index == match.index), None
        )
        if block is None:  # defensive — validity already filtered above
            continue
        claim = _claim_for(match.start)
        quoted = find_quoted_span(
            block.content, claim,
            max_full_quote_chars=quoted_span_max_chars,
        )
        citations.append(
            ResolvedCitation(
                index=match.index,
                chunk_id=block.chunk_id,
                document_id=block.document_id,
                document_version_id=block.document_version_id,
                document_name=block.document_name,
                page_id=block.page_id,
                page_number=block.page_number,
                section=block.section_title,
                relevance=block.relevance,
                quoted=quoted,
                claim_text=claim,
            )
        )

    return CitationExtraction(
        citations=citations,
        cleaned_text=cleaned_text,
        invalid_indices=invalid_indices,
    )
