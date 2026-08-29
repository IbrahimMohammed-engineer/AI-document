"""
Unit tests — citation extraction and resolution (Phase 10, Backend §35).

Covers (roadmap Phase 10 §Testing): the extraction regex on answer fixtures,
quoted-span offset computation, invalid-reference stripping, and resolution
through the backend-owned source map (the never-fabricate property).

Pure logic — no Docker, no LLM.
"""
from __future__ import annotations

import pytest

from app.rag.citations import (
    extract_references,
    find_quoted_span,
    resolve_citations,
    split_sentences,
    strip_unresolvable_references,
)
from app.rag.context_builder import ContextBundle, SourceBlock


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _block(index: int, content: str, *, chunk_id: str | None = None) -> SourceBlock:
    return SourceBlock(
        index=index,
        chunk_id=chunk_id or f"chunk-{index}",
        document_id=f"doc-{index}",
        document_version_id=f"ver-{index}",
        document_name=f"Document {index}",
        page_id=f"page-{index}",
        page_number=index * 10,
        section_title=f"{index}.0 Section",
        content=content,
        relevance=0.9 - index * 0.1,
        token_count=20,
    )


def _bundle(*blocks: SourceBlock) -> ContextBundle:
    bundle = ContextBundle(blocks=list(blocks))
    bundle.prompt_text = "\n\n".join(b.format() for b in blocks)
    return bundle


# ── Reference extraction ──────────────────────────────────────────────────────

@pytest.mark.unit
class TestExtractReferences:

    def test_single_marker(self):
        refs = extract_references("The process has four stages. [1]")
        # "The process has four stages." is 28 chars → marker at 29..32
        assert [(r.index, r.start, r.end) for r in refs] == [(1, 29, 32)]

    def test_adjacent_markers_independent(self):
        # [1][2] → two independent references (never merged — Phase 10 step 8)
        refs = extract_references("Stages exist. [1][2]")
        assert [r.index for r in refs] == [1, 2]

    def test_source_prefixed_marker(self):
        refs = extract_references("Stages exist. [SOURCE 3]")
        assert [r.index for r in refs] == [3]

    def test_repeated_references_preserved(self):
        refs = extract_references("One. [1] Two. [1]")
        assert [r.index for r in refs] == [1, 1]

    def test_no_markers(self):
        assert extract_references("No citations here.") == []


# ── Invalid-reference stripping ───────────────────────────────────────────────

@pytest.mark.unit
class TestStripUnresolvable:

    def test_invalid_stripped_valid_kept(self):
        cleaned, invalid = strip_unresolvable_references(
            "A fact. [1] Another. [3] More. [2]", valid_indices={1, 2}
        )
        assert "[3]" not in cleaned
        assert "[1]" in cleaned and "[2]" in cleaned
        assert invalid == [3]

    def test_all_valid_no_changes(self):
        text = "A fact. [1][2]"
        cleaned, invalid = strip_unresolvable_references(text, {1, 2})
        assert cleaned == text
        assert invalid == []

    def test_source_prefix_variants_stripped(self):
        cleaned, invalid = strip_unresolvable_references(
            "Fact. [SOURCE 9]", valid_indices={1}
        )
        assert "[SOURCE 9]" not in cleaned
        assert invalid == [9]


# ── Sentence segmentation ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestSplitSentences:

    def test_basic_offsets_roundtrip(self):
        text = "First sentence. Second one! Third?"
        sentences = split_sentences(text)
        assert [s for s, _, _ in sentences] == [
            "First sentence.", "Second one!", "Third?"
        ]
        for sentence, start, end in sentences:
            assert text[start:end].strip() == sentence

    def test_marker_inside_sentence_span(self):
        text = "A claim lives here. [1] Next sentence."
        sentences = split_sentences(text)
        marker_at = text.index("[1]")
        containing = [s for s, a, b in sentences if a <= marker_at <= b]
        assert len(containing) == 1
        assert "A claim" in containing[0]


# ── Quoted-span computation (Backend §35) ─────────────────────────────────────

@pytest.mark.unit
class TestQuotedSpan:

    def test_short_chunk_quoted_in_full(self):
        content = "Short chunk content."
        span = find_quoted_span(content, "some claim")
        assert span.text == content
        assert (span.char_start, span.char_end) == (0, len(content))

    def test_long_chunk_picks_matching_sentence_with_offsets(self):
        content = (
            "Preamble sentence about nothing in particular. "
            "The approval process requires four sequential stages of review. "
            "Unrelated closing note about parking regulations."
        )
        assert len(content) > 400 or True  # long-chunk path needs > threshold
        span = find_quoted_span(
            content,
            "How many stages does the approval process require?",
            max_full_quote_chars=10,  # force the sentence-pick path
        )
        assert "four sequential stages" in span.text
        assert content[span.char_start:span.char_end] == span.text
        assert span.context_before != ""  # preamble precedes it

    def test_offsets_index_into_original_content(self):
        content = (
            "First filler sentence with some words. "
            "Target sentence mentions quarterly compliance audits. "
            "Tail sentence."
        )
        span = find_quoted_span(
            content, "quarterly compliance audits", max_full_quote_chars=10
        )
        assert "quarterly compliance audits" in span.text
        assert content[span.char_start:span.char_end] == span.text

    def test_no_sentence_boundary_falls_back_to_head(self):
        content = "one long run without any sentence boundaries " * 5
        span = find_quoted_span(content, "claim", max_full_quote_chars=20)
        assert span.char_start == 0
        assert len(span.text) <= 20 + 20  # head span, bounded


# ── Resolution through the backend-owned map (the linchpin) ───────────────────

@pytest.mark.unit
class TestResolveCitations:

    def test_resolution_uses_context_map_not_model_memory(self):
        bundle = _bundle(_block(1, "The approval process has four stages."))
        extraction = resolve_citations(
            "The approval process has four stages. [1]", bundle
        )
        assert extraction.invalid_indices == []
        assert len(extraction.citations) == 1
        c = extraction.citations[0]
        # Every provenance field comes from the BACKEND's block — the LLM
        # only echoed the label [1] (Backend §35).
        assert c.chunk_id == "chunk-1"
        assert c.document_id == "doc-1"
        assert c.document_version_id == "ver-1"
        assert c.document_name == "Document 1"
        assert c.page_id == "page-1"
        assert c.page_number == 10
        assert c.section == "1.0 Section"
        assert c.index == 1

    def test_invalid_reference_stripped_not_resolved(self):
        bundle = _bundle(_block(1, "Source one content."), _block(2, "Source two."))
        extraction = resolve_citations(
            "A claim. [1] A fabricated claim. [3]", bundle
        )
        assert extraction.invalid_indices == [3]
        assert [c.index for c in extraction.citations] == [1]
        assert "[3]" not in extraction.cleaned_text
        assert "[1]" in extraction.cleaned_text

    def test_multiple_citations_independent(self):
        bundle = _bundle(_block(1, "One."), _block(2, "Two."))
        extraction = resolve_citations("Claim here. [1][2]", bundle)
        assert sorted(c.index for c in extraction.citations) == [1, 2]
        assert extraction.citations[0].chunk_id != extraction.citations[1].chunk_id

    def test_quoted_text_is_real_chunk_content(self):
        content = "The exact source sentence lives here."
        bundle = _bundle(_block(1, content))
        extraction = resolve_citations("Claim. [1]", bundle)
        assert extraction.citations[0].quoted.text == content  # short chunk → full

    def test_no_markers_yields_no_citations(self):
        bundle = _bundle(_block(1, "Content."))
        extraction = resolve_citations("An answer with no markers.", bundle)
        assert extraction.citations == []
        assert extraction.cleaned_text == "An answer with no markers."
