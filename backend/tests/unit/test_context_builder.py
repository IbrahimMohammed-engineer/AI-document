"""
Unit tests — context builder (Phase 9, Backend §33).

Covers (roadmap Phase 9 §Testing): budget truncation, dedup, ordering, and
label/map integrity — including the security property that chunk ids never
appear in prompt text.
"""
from __future__ import annotations

import pytest

from app.rag.context_builder import build_context, dedup_sources
from app.rag.retriever import SearchResult


def _result(
    chunk_id: str,
    content: str,
    relevance: float,
    *,
    document_name: str = "Marketing Policy 2026",
    page_number: int = 12,
    section_title: str | None = "4.2 Regulatory Review",
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        document_version_id=f"ver-{chunk_id}",
        document_name=document_name,
        page_id=f"page-{chunk_id}",
        page_number=page_number,
        section_title=section_title,
        chunk_index=0,
        snippet=content[:500],
        content=content,
        token_count=10,
        relevance=relevance,
        embedding_model="stub",
        metadata={},
    )


@pytest.mark.unit
class TestOrdering:

    def test_sources_ordered_by_relevance_desc(self):
        results = [
            _result("c-low", "low relevance content", 0.4),
            _result("c-high", "high relevance content", 0.9),
            _result("c-mid", "mid relevance content", 0.6),
        ]
        bundle = build_context(results)
        assert [b.chunk_id for b in bundle.blocks] == ["c-high", "c-mid", "c-low"]
        assert [b.index for b in bundle.blocks] == [1, 2, 3]

    def test_source_label_format(self):
        bundle = build_context([_result("c1", "the content", 0.9)])
        assert (
            "SOURCE 1" in bundle.prompt_text
            and "Document: Marketing Policy 2026" in bundle.prompt_text
            and "Page: 12" in bundle.prompt_text
            and "Section: 4.2 Regulatory Review" in bundle.prompt_text
            and '"""' in bundle.prompt_text
        )


@pytest.mark.unit
class TestBudget:

    def test_sources_added_until_budget(self):
        # Distinct contents — identical ones would be deduplicated instead
        results = [
            _result(f"c{i}", f"distinct chunk number {i} " + "token " * 380,
                    relevance=1.0 - i * 0.1)
            for i in range(6)
        ]
        bundle = build_context(results, budget_tokens=1000)
        assert 0 < len(bundle.blocks) < 6
        assert bundle.total_tokens <= 1000
        assert bundle.dropped_by_budget == 6 - len(bundle.blocks)

    def test_oversized_first_chunk_truncated_not_dropped(self):
        huge = " ".join(["word"] * 5000)
        bundle = build_context([_result("c-huge", huge, 0.9)], budget_tokens=500)
        assert len(bundle.blocks) == 1
        assert "[truncated]" in bundle.blocks[0].content
        assert bundle.total_tokens <= 500 + 50  # small formatting overhead slack

    def test_small_budget_keeps_at_least_one_source(self):
        results = [_result("c1", "short content", 0.9)]
        bundle = build_context(results, budget_tokens=30)
        assert len(bundle.blocks) == 1


@pytest.mark.unit
class TestDedup:

    def test_exact_duplicates_after_normalization(self):
        a = _result("a", "The approval process has four stages.", 0.9)
        b = _result("b", "the  approval\nprocess has FOUR stages.  ", 0.5)
        kept, deduped = dedup_sources([a, b])
        assert deduped == 1
        assert [r.chunk_id for r in kept] == ["a"]  # higher-scored wins

    def test_contained_chunk_dropped_regardless_of_order(self):
        long = _result("long", "one two three four five six seven eight", 0.6)
        short = _result("short", "three four five", 0.9)  # higher score, contained
        kept, deduped = dedup_sources([short, long])
        assert deduped == 1
        assert [r.chunk_id for r in kept] == ["short"]

    def test_distinct_chunks_all_kept(self):
        results = [
            _result("a", "alpha content", 0.9),
            _result("b", "beta content", 0.8),
        ]
        kept, deduped = dedup_sources(results)
        assert deduped == 0
        assert len(kept) == 2

    def test_empty_content_dropped(self):
        kept, deduped = dedup_sources([_result("a", "   ", 0.9)])
        assert deduped == 1
        assert kept == []


@pytest.mark.unit
class TestLabelMapIntegrity:

    def test_chunk_ids_never_in_prompt_text(self):
        results = [
            _result("secret-chunk-uuid-1", "alpha content", 0.9),
            _result("secret-chunk-uuid-2", "beta content", 0.8),
        ]
        bundle = build_context(results)
        assert "secret-chunk-uuid-1" not in bundle.prompt_text
        assert "secret-chunk-uuid-2" not in bundle.prompt_text
        assert bundle.source_index == {1: "secret-chunk-uuid-1", 2: "secret-chunk-uuid-2"}

    def test_metadata_round_trips_unchanged(self):
        result = _result(
            "c1", "content", 0.9,
            document_name="Vendor Contract", page_number=8,
            section_title="3 Payment Terms",
        )
        bundle = build_context([result])
        block = bundle.blocks[0]
        assert block.document_id == result.document_id
        assert block.document_version_id == result.document_version_id
        assert block.document_name == "Vendor Contract"
        assert block.page_number == 8
        assert block.section_title == "3 Payment Terms"
        assert block.content == "content"
        assert block.relevance == 0.9

    def test_empty_input_yields_empty_bundle(self):
        bundle = build_context([])
        assert bundle.blocks == []
        assert bundle.prompt_text == ""
        assert bundle.source_index == {}
