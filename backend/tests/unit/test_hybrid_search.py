"""
Unit tests for Phase 8 hybrid search — pure logic, no Docker required.

Covers the roadmap Phase 8 testing requirements that are unit-testable:
  - RRF math: known rankings → known fused order (Backend §31 / DB §18)
  - RRF dampening: a chunk both branches agree on beats a single-branch #1
  - min-max normalization (keyword presentation scores)
  - Threshold filtering: candidates below the rerank threshold are dropped
    EVEN IF within top-K by rank (Backend §32 — honest-empty guarantee)
  - Reranker fallback: provider disabled/failed → fused order preserved,
    used_reranker=False, request never fails (Backend §32/§51)
  - Mode-toggle branch skipping: keyword never reranks nor embeds;
    semantic skips the FTS branch; hybrid runs both (Backend §39)
  - SearchFilters sanitization (unknown document_type values)

Integration/API coverage lives in tests/integration/test_hybrid_search_pipeline.py
and tests/api/test_search.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.domain.search import SearchFilters
from app.infrastructure.embeddings import StubEmbeddingProvider
from app.infrastructure.reranker import (
    RerankerProvider,
    RerankerProviderError,
    StubRerankerProvider,
)
from app.rag.hybrid_search import (
    HybridRetriever,
    normalize_minmax,
    rrf_fuse,
)
from app.rag.reranker import rerank_candidates
from app.rag.retriever import SearchResult
from app.repositories.document_chunk_repository import ChunkSearchResult

# ── Builders ──────────────────────────────────────────────────────────────────


def make_chunk(chunk_id: str, content: str = "content") -> ChunkSearchResult:
    return ChunkSearchResult(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        document_version_id=f"ver-{chunk_id}",
        document_name=f"Doc {chunk_id}",
        page_id=f"page-{chunk_id}",
        page_number=1,
        section_title=None,
        chunk_index=0,
        content=content,
        token_count=10,
        similarity=0.9,  # branch raw score (cosine or ts_rank) — not used by RRF
        embedding_model="stub",
        metadata={},
    )


def make_result(
    chunk_id: str,
    content: str = "content",
    relevance: float = 0.5,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        document_version_id=f"ver-{chunk_id}",
        document_name=f"Doc {chunk_id}",
        page_id=f"page-{chunk_id}",
        page_number=1,
        section_title=None,
        chunk_index=0,
        snippet=content[:500],
        content=content,
        token_count=10,
        relevance=relevance,
        embedding_model=None,
        metadata={},
    )


class ScriptedReranker(RerankerProvider):
    """Returns a fixed score per chunk_id — lets tests script exact orders."""

    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    @property
    def model_name(self) -> str:
        return "scripted"

    async def rerank(self, query, documents):
        # Scores keyed by the candidates' content marker (chunk id is embedded
        # in content by the tests: "content about X" — match by substring).
        out = []
        for doc in documents:
            score = 0.0
            for marker, value in self._scores.items():
                if marker in doc:
                    score = value
                    break
            out.append(score)
        return out


class ExplodingReranker(RerankerProvider):
    """Always raises — the fallback path must swallow this."""

    @property
    def model_name(self) -> str:
        return "exploding"

    async def rerank(self, query, documents):
        raise RerankerProviderError("provider down", code="RERANKER_PROVIDER_ERROR")


# ── RRF fusion math ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRRFFusion:

    def test_known_rankings_known_fused_order(self):
        """vector=[A,B,C], keyword=[B,D,A], k=60 → order B, A, D, C.

        A = 1/61 + 1/63 = 0.032266…
        B = 1/62 + 1/61 = 0.032522…   ← both branches agree → wins
        C = 1/63           = 0.015873…
        D = 1/62           = 0.016129…
        """
        vector = [make_chunk("A"), make_chunk("B"), make_chunk("C")]
        keyword = [make_chunk("B"), make_chunk("D"), make_chunk("A")]

        fused = rrf_fuse(vector, keyword, k=60)
        order = [c.chunk.chunk_id for c in fused]

        assert order == ["B", "A", "D", "C"]

    def test_fused_scores_match_formula(self):
        vector = [make_chunk("A")]
        keyword = [make_chunk("A")]
        fused = rrf_fuse(vector, keyword, k=60)
        assert len(fused) == 1
        expected = 1.0 / 61 + 1.0 / 61
        assert fused[0].fused_score == pytest.approx(expected)

    def test_ranks_recorded_per_branch(self):
        vector = [make_chunk("A"), make_chunk("B")]
        keyword = [make_chunk("B")]

        fused = {c.chunk.chunk_id: c for c in rrf_fuse(vector, keyword, k=60)}
        assert fused["A"].vector_rank == 1
        assert fused["A"].keyword_rank is None
        assert fused["B"].vector_rank == 2
        assert fused["B"].keyword_rank == 1

    def test_agreement_beats_single_branch_top(self):
        """The dampening property: k=60 stops a #1 on ONE branch from
        beating a chunk that BOTH branches rank highly."""
        # X is #1 on vector only; Y is #10 on both branches
        vector = [make_chunk("X")] + [make_chunk(f"f{i}") for i in range(8)] + [make_chunk("Y")]
        keyword = [make_chunk(f"k{i}") for i in range(9)] + [make_chunk("Y")]

        fused = rrf_fuse(vector, keyword, k=60)
        order = [c.chunk.chunk_id for c in fused]

        assert order.index("Y") < order.index("X"), (
            "a both-branch chunk must outrank a single-branch #1 (k=60 dampening)"
        )

    def test_custom_k(self):
        vector = [make_chunk("A")]
        fused = rrf_fuse(vector, [], k=10)
        assert fused[0].fused_score == pytest.approx(1.0 / 11)

    def test_disjoint_sets_union(self):
        fused = rrf_fuse([make_chunk("A")], [make_chunk("B")], k=60)
        assert {c.chunk.chunk_id for c in fused} == {"A", "B"}

    def test_empty_inputs(self):
        assert rrf_fuse([], [], k=60) == []
        fused = rrf_fuse([make_chunk("A")], [], k=60)
        assert [c.chunk.chunk_id for c in fused] == ["A"]


# ── Presentation-score normalization ──────────────────────────────────────────

@pytest.mark.unit
class TestNormalizeMinmax:

    def test_basic_spread(self):
        assert normalize_minmax([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]

    def test_all_equal_maps_to_one(self):
        """A set of equally-matching hits is a full-strength match set."""
        assert normalize_minmax([0.7, 0.7, 0.7]) == [1.0, 1.0, 1.0]

    def test_empty(self):
        assert normalize_minmax([]) == []

    def test_order_preserved(self):
        values = [5.0, 1.0, 3.0]
        normalized = normalize_minmax(values)
        assert normalized[0] > normalized[2] > normalized[1]


# ── Rerank stage: threshold + fallback ────────────────────────────────────────

@pytest.mark.unit
class TestRerankCandidates:

    def test_provider_none_falls_back_to_fused_order(self):
        candidates = [make_result("a"), make_result("b")]
        outcome = asyncio.run(
            rerank_candidates("query", candidates, provider=None, threshold=0.35)
        )
        assert outcome.used_reranker is False
        assert outcome.results == candidates  # same objects, same order
        assert outcome.dropped_count == 0

    def test_provider_error_falls_back_never_raises(self):
        candidates = [make_result("a"), make_result("b")]
        outcome = asyncio.run(
            rerank_candidates(
                "query", candidates, provider=ExplodingReranker(), threshold=0.35
            )
        )
        assert outcome.used_reranker is False
        assert outcome.results == candidates
        assert outcome.reranker_error == "RERANKER_PROVIDER_ERROR"

    def test_below_threshold_dropped_even_if_top_ranked(self):
        """Backend §32: threshold beats rank — a low-scoring candidate is
        dropped even though fusion ranked it first."""
        candidates = [make_result("first", content="alpha"), make_result("second", content="beta")]
        reranker = ScriptedReranker({"alpha": 0.1, "beta": 0.9})
        outcome = asyncio.run(
            rerank_candidates("query", candidates, provider=reranker, threshold=0.35)
        )
        assert outcome.used_reranker is True
        assert outcome.dropped_count == 1
        assert [r.chunk_id for r in outcome.results] == ["second"]

    def test_scores_reorder_candidates(self):
        """Fused order is b, a — reranker prefers a → final order a, b."""
        candidates = [make_result("b", content="bbb"), make_result("a", content="aaa")]
        reranker = ScriptedReranker({"aaa": 0.8, "bbb": 0.4})
        outcome = asyncio.run(
            rerank_candidates("query", candidates, provider=reranker, threshold=0.35)
        )
        assert [r.chunk_id for r in outcome.results] == ["a", "b"]
        # Relevance presentation = reranker score (Backend §39)
        assert outcome.results[0].relevance == pytest.approx(0.8)
        assert outcome.results[0].rerank_score == pytest.approx(0.8)

    def test_score_count_mismatch_falls_back(self):
        candidates = [make_result("a"), make_result("b")]

        class ShortReranker(RerankerProvider):
            """Returns fewer scores than documents — a misbehaving provider."""

            @property
            def model_name(self) -> str:
                return "short"

            async def rerank(self, query, documents):
                return [0.9]  # one score for two documents

        outcome = asyncio.run(
            rerank_candidates("query", candidates, provider=ShortReranker(), threshold=0.35)
        )
        assert outcome.used_reranker is False
        assert outcome.reranker_error == "RERANKER_SCORE_COUNT_MISMATCH"
        assert outcome.results == candidates

    def test_all_below_threshold_returns_empty(self):
        """The honest-empty outcome: zero usable chunks is reachable."""
        candidates = [make_result("a", content="x"), make_result("b", content="y")]
        reranker = ScriptedReranker({"x": 0.1, "y": 0.2})
        outcome = asyncio.run(
            rerank_candidates("query", candidates, provider=reranker, threshold=0.35)
        )
        assert outcome.results == []
        assert outcome.dropped_count == 2

    def test_empty_candidates(self):
        outcome = asyncio.run(
            rerank_candidates("query", [], provider=StubRerankerProvider(), threshold=0.35)
        )
        assert outcome.results == []
        assert outcome.used_reranker is False


# ── Mode-toggle branch skipping (Backend §39) ─────────────────────────────────

@dataclass
class FakeUser:
    id: str = "user-1"
    organization_id: str = "org-1"


class RecordingRepo:
    """Stands in for DocumentChunkRepository; records which branches ran."""

    def __init__(self, vector_rows=None, keyword_rows=None) -> None:
        self.vector_rows = vector_rows or []
        self.keyword_rows = keyword_rows or []
        self.semantic_calls = 0
        self.keyword_calls = 0

    async def semantic_search(self, **kwargs):
        self.semantic_calls += 1
        return list(self.vector_rows)

    async def keyword_search(self, **kwargs):
        self.keyword_calls += 1
        return list(self.keyword_rows)


@pytest.mark.unit
class TestModeToggle:

    def _patch(self, monkeypatch, repo: RecordingRepo, allowed: list[str]):
        import app.rag.hybrid_search as hs

        class FakeAuth:
            @staticmethod
            async def resolve_allowed_documents(user, db, *, scope=None):
                return allowed

        monkeypatch.setattr(hs, "DocumentChunkRepository", lambda db: repo)
        monkeypatch.setattr(hs, "AuthorizationService", FakeAuth)

    def test_keyword_mode_skips_vector_and_rerank(self, monkeypatch):
        repo = RecordingRepo(
            vector_rows=[make_chunk("v1", "retention policy")],
            keyword_rows=[make_chunk("k1", "retention policy seven days")],
        )
        self._patch(monkeypatch, repo, allowed=["ver-1"])

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
            reranker_provider=StubRerankerProvider(),
        )
        outcome = asyncio.run(
            retriever.search("retention policy", FakeUser(), mode="keyword")
        )

        assert outcome.mode == "keyword"
        assert repo.semantic_calls == 0, "keyword mode must NOT hit the vector branch"
        assert repo.keyword_calls == 1
        assert outcome.used_reranker is False, "keyword mode never reranks (Backend §39)"
        assert [r.chunk_id for r in outcome.results] == ["k1"]
        # Presentation: normalized ts_rank, not raw rank
        assert all(0.0 <= r.relevance <= 1.0 for r in outcome.results)

    def test_semantic_mode_skips_keyword_branch(self, monkeypatch):
        # Content shares every query term → stub reranker scores it 1.0
        # (above the 0.35 default threshold) so it survives reranking.
        repo = RecordingRepo(
            vector_rows=[make_chunk("v1", "how long does approval take seven business days")],
            keyword_rows=[make_chunk("k1", "how long does approval take seven business days")],
        )
        self._patch(monkeypatch, repo, allowed=["ver-1"])

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
            reranker_provider=StubRerankerProvider(),
        )
        outcome = asyncio.run(
            retriever.search("how long does approval take", FakeUser(), mode="semantic")
        )

        assert outcome.mode == "semantic"
        assert repo.semantic_calls == 1
        assert repo.keyword_calls == 0, "semantic mode must NOT hit the FTS branch"
        assert outcome.used_reranker is True  # semantic candidates ARE reranked
        assert [r.chunk_id for r in outcome.results] == ["v1"]

    def test_hybrid_mode_runs_both_branches(self, monkeypatch):
        repo = RecordingRepo(
            vector_rows=[make_chunk("v1", "section 4.2 escalation clause")],
            keyword_rows=[make_chunk("k1", "section 4.2 escalation clause")],
        )
        self._patch(monkeypatch, repo, allowed=["ver-1"])

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
            reranker_provider=StubRerankerProvider(),
        )
        outcome = asyncio.run(
            retriever.search("section 4.2 escalation", FakeUser(), mode="hybrid")
        )

        assert outcome.mode == "hybrid"
        assert repo.semantic_calls == 1
        assert repo.keyword_calls == 1
        assert outcome.fused_candidate_count == 2  # A + B fused (disjoint ids)
        assert outcome.used_reranker is True

    def test_empty_scope_short_circuits_all_branches(self, monkeypatch):
        repo = RecordingRepo()
        self._patch(monkeypatch, repo, allowed=[])  # no allowed versions

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
            reranker_provider=StubRerankerProvider(),
        )
        for mode in ("hybrid", "semantic", "keyword"):
            outcome = asyncio.run(
                retriever.search("anything", FakeUser(), mode=mode)
            )
            assert outcome.results == []
        assert repo.semantic_calls == 0 and repo.keyword_calls == 0

    def test_reranker_fallback_in_hybrid_serves_fused_order(self, monkeypatch):
        repo = RecordingRepo(
            vector_rows=[make_chunk("v1", "policy text one")],
            keyword_rows=[make_chunk("k1", "policy text two")],
        )
        self._patch(monkeypatch, repo, allowed=["ver-1"])

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
            reranker_provider=ExplodingReranker(),  # outage
        )
        outcome = asyncio.run(
            retriever.search("policy text", FakeUser(), mode="hybrid")
        )

        assert outcome.used_reranker is False, "outage degrades, never fails"
        assert outcome.reranker_note == "RERANKER_PROVIDER_ERROR"
        assert len(outcome.results) == 2  # unreranked fused candidates served
        # Fallback presentation = normalized fused score, not raw cosine
        assert all(0.0 <= r.relevance <= 1.0 for r in outcome.results)

    def test_top_k_truncates_final_results(self, monkeypatch):
        repo = RecordingRepo(
            keyword_rows=[make_chunk(f"k{i}") for i in range(10)],
        )
        self._patch(monkeypatch, repo, allowed=["ver-1"])

        retriever = HybridRetriever(
            None,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
        )
        outcome = asyncio.run(
            retriever.search("query", FakeUser(), mode="keyword", top_k=3)
        )
        assert len(outcome.results) == 3


# ── SearchFilters sanitization ────────────────────────────────────────────────

@pytest.mark.unit
class TestSearchFilters:

    def test_empty_predicate(self):
        assert SearchFilters.empty().is_empty is True
        assert SearchFilters().is_empty is True

    def test_non_empty_predicate(self):
        assert SearchFilters(department="Legal").is_empty is False
        assert SearchFilters(document_types=("policy",)).is_empty is False

    def test_sanitized_drops_unknown_types(self):
        filters = SearchFilters(document_types=("policy", "not-a-type"))
        cleaned = filters.sanitized()
        assert cleaned.document_types == ("policy",)

    def test_sanitized_keeps_unknown_only_sets(self):
        """If ONLY unknown types were supplied, keep them — the query must
        legitimately match nothing rather than silently ignoring the filter."""
        filters = SearchFilters(document_types=("bogus",))
        assert filters.sanitized().document_types == ("bogus",)

    def test_sanitized_preserves_other_dimensions(self):
        filters = SearchFilters(
            document_types=("policy",),
            department="HR",
            owner_id="u1",
            collection_ids=("c1",),
        )
        cleaned = filters.sanitized()
        assert cleaned == filters
