"""
Unit tests — candidate generation pure logic (Phase 13, plan §23 Task 5).

Asserts the deterministic filter chain of
``ConflictService.generate_candidates_for_chunk`` against a STUBBED chunk
repository (no DB):
  - a chunk from the excluded (same) document never appears;
  - results below CANDIDATE_SIMILARITY_THRESHOLD are dropped;
  - the pair-ordering rule yields each unordered pair at most once
    (chunk_b.id > chunk_a.id);
  - an un-embedded source chunk produces no candidates;
  - CANDIDATE_TOP_K is respected (stub returns exactly top_k results).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import pytest

from app.domain.conflict_rules import CANDIDATE_SIMILARITY_THRESHOLD, CANDIDATE_TOP_K
from app.services.conflict_service import (
    CandidatePair,
    ConflictService,
    StatementChunk,
    _section_label,
    _statement_chunk,
)

pytestmark = pytest.mark.unit


def _chunk(document_id: str, text: str = "chunk") -> StatementChunk:
    # chunk_id starts with a mid-range hex digit ("8") so "0"-prefixed ids
    # deterministically sort BELOW it and "f"-prefixed ids ABOVE it (the
    # pair-ordering rule compares chunk ids as strings).
    return StatementChunk(
        chunk_id="8" + uuid.uuid4().hex[1:],
        document_id=document_id,
        document_version_id=str(uuid.uuid4()),
        page_id=str(uuid.uuid4()),
        page_number=1,
        section_title="Section",
        content=text,
    )


@dataclass
class FakeSearchResult:
    chunk_id: str
    document_id: str
    document_version_id: str
    page_id: str
    page_number: int
    section_title: Optional[str] = None
    content: str = "result chunk"
    similarity: float = 0.0


@dataclass
class StubChunkRepo:
    """Records the semantic_search call; returns scripted results."""

    embedding: Optional[list[float]] = field(default=None)
    results: list[FakeSearchResult] = field(default_factory=list)
    exclude_seen: Optional[str] = None
    top_k_seen: Optional[int] = None
    search_calls: int = 0

    async def get_chunk_embedding(self, chunk_id: str):
        return self.embedding

    async def semantic_search(self, *, organization_id, version_ids, query_vector,
                              top_k=50, hnsw_ef_search=100, filters=None,
                              exclude_document_id=None):
        self.search_calls += 1
        self.exclude_seen = exclude_document_id
        self.top_k_seen = top_k
        return self.results


ORG = str(uuid.uuid4())
DOC_A = str(uuid.uuid4())
DOC_B = str(uuid.uuid4())
VERSION_IDS = [str(uuid.uuid4()), str(uuid.uuid4())]


async def _collect(repo, chunk_a):
    out = []
    async for pair in ConflictService.generate_candidates_for_chunk(
        chunk_a,
        organization_id=ORG,
        version_ids=VERSION_IDS,
        chunk_repo=repo,
        db=None,  # unused by this code path
    ):
        out.append(pair)
    return out


class TestGenerateCandidatesForChunk:

    @pytest.mark.asyncio
    async def test_unembedded_source_chunk_yields_nothing(self):
        repo = StubChunkRepo(embedding=None)
        pairs = await _collect(repo, _chunk(DOC_A))
        assert pairs == []
        assert repo.search_calls == 0  # short-circuited before the ANN query

    @pytest.mark.asyncio
    async def test_same_document_results_excluded_via_query_predicate(self):
        repo = StubChunkRepo(embedding=[1.0], results=[])
        await _collect(repo, _chunk(DOC_A))
        assert repo.exclude_seen == DOC_A  # exclude_document_id always passed
        assert repo.top_k_seen == CANDIDATE_TOP_K

    @pytest.mark.asyncio
    async def test_below_threshold_similarity_dropped(self):
        source = _chunk(DOC_A)
        other = _chunk(DOC_B)
        repo = StubChunkRepo(embedding=[1.0], results=[
            FakeSearchResult(
                chunk_id=other.chunk_id, document_id=DOC_B,
                document_version_id=str(uuid.uuid4()), page_id=str(uuid.uuid4()),
                page_number=1,
                similarity=CANDIDATE_SIMILARITY_THRESHOLD - 0.01,
            ),
        ])
        pairs = await _collect(repo, source)
        assert pairs == []

    @pytest.mark.asyncio
    async def test_at_threshold_similarity_kept(self):
        source = _chunk(DOC_A)
        other = FakeSearchResult(
            # deterministically sorts AFTER the source's id ("9..." > "8...")
            chunk_id="9" + uuid.uuid4().hex[1:], document_id=DOC_B,
            document_version_id=str(uuid.uuid4()), page_id=str(uuid.uuid4()),
            page_number=1,
            similarity=CANDIDATE_SIMILARITY_THRESHOLD,
        )
        repo = StubChunkRepo(embedding=[1.0], results=[other])
        pairs = await _collect(repo, source)
        assert len(pairs) == 1
        assert isinstance(pairs[0], CandidatePair)
        assert pairs[0].similarity == CANDIDATE_SIMILARITY_THRESHOLD

    @pytest.mark.asyncio
    async def test_pair_ordering_rule_deduplicates_directions(self):
        """chunk_b.id > chunk_a.id only — the (A,B)/(B,A) duplicate direction
        is filtered deterministically (§10)."""
        source = _chunk(DOC_A)
        lower_id = _chunk(DOC_B)
        # Force the "other" chunk's id to sort BEFORE the source's id
        lower = FakeSearchResult(
            chunk_id="0" + source.chunk_id[1:], document_id=DOC_B,
            document_version_id=str(uuid.uuid4()), page_id=str(uuid.uuid4()),
            page_number=1, similarity=0.99,
        )
        higher = FakeSearchResult(
            chunk_id="f" + source.chunk_id[1:], document_id=DOC_B,
            document_version_id=str(uuid.uuid4()), page_id=str(uuid.uuid4()),
            page_number=1, similarity=0.99,
        )
        repo = StubChunkRepo(embedding=[1.0], results=[lower, higher])
        pairs = await _collect(repo, source)
        kept_ids = {p.chunk_b.chunk_id for p in pairs}
        assert lower.chunk_id not in kept_ids
        assert higher.chunk_id in kept_ids

    @pytest.mark.asyncio
    async def test_normalization_maps_search_result_fields(self):
        source = _chunk(DOC_A, "source text")
        other_doc = DOC_B
        other_version = str(uuid.uuid4())
        other_page = str(uuid.uuid4())
        result = FakeSearchResult(
            # deterministically sorts AFTER the source's id (pair-ordering rule)
            chunk_id="f" + source.chunk_id[1:], document_id=other_doc,
            document_version_id=other_version, page_id=other_page,
            page_number=7, section_title="Leave Procedures",
            content="All vacation requests require HR approval.",
            similarity=0.95,
        )
        repo = StubChunkRepo(embedding=[1.0], results=[result])
        pairs = await _collect(repo, source)
        assert len(pairs) == 1
        chunk_b = pairs[0].chunk_b
        assert chunk_b.chunk_id == result.chunk_id
        assert chunk_b.document_id == other_doc
        assert chunk_b.document_version_id == other_version
        assert chunk_b.page_id == other_page
        assert chunk_b.page_number == 7
        assert chunk_b.content == "All vacation requests require HR approval."


class TestStatementChunkNormalization:

    def test_projection_dict_shape(self):
        normalized = _statement_chunk({
            "chunk_id": str(uuid.uuid4()),
            "document_id": DOC_A,
            "document_version_id": str(uuid.uuid4()),
            "page_id": str(uuid.uuid4()),
            "page_number": 3,
            "section_title": "Approval Process",
            "section_number": "3.1",
            "content": "text",
        })
        assert normalized.section_number == "3.1"
        assert normalized.section_title == "Approval Process"

    def test_provenance_dict_shape(self):
        """ComparisonService.resolve_chunk_provenance shape (Phase 12 join)."""
        normalized = _statement_chunk({
            "chunk_id": str(uuid.uuid4()),
            "document_id": DOC_A,
            "document_version_id": str(uuid.uuid4()),
            "page_id": str(uuid.uuid4()),
            "page_number": 2,
            "section": "3.1 Approval Process",
            "content": "text",
        })
        assert normalized.section_title == "3.1 Approval Process"
        assert normalized.section_number is None

    def test_section_label_composition(self):
        chunk = StatementChunk(
            chunk_id=str(uuid.uuid4()), document_id=DOC_A,
            document_version_id=str(uuid.uuid4()), page_id=str(uuid.uuid4()),
            page_number=1, section_title="Approval Process",
            section_number="3.1", content="",
        )
        assert _section_label(chunk) == "3.1 Approval Process"
