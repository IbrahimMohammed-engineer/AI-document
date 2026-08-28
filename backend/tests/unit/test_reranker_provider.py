"""
Unit tests for the reranker provider abstraction (Phase 8) — no Docker, no API.

Covers:
  - StubRerankerProvider: deterministic lexical-overlap scoring
  - Score normalization (already-bounded clamping / sigmoid squash)
  - CohereRerankerProvider construction guards (missing SDK → typed error)
  - init_reranker_provider selection: none / stub / cohere-without-key
  - Process-global get/set contract
"""
from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.reranker import (
    CohereRerankerProvider,
    RerankerProviderError,
    StubRerankerProvider,
    get_reranker_provider,
    init_reranker_provider,
    normalize_scores,
    set_reranker_provider,
)


@pytest.mark.unit
class TestStubRerankerProvider:

    def test_deterministic(self):
        provider = StubRerankerProvider()
        first = asyncio.run(provider.rerank("retention policy", ["retention policy for documents", "unrelated"]))
        second = asyncio.run(provider.rerank("retention policy", ["retention policy for documents", "unrelated"]))
        assert first == second

    def test_overlap_ordering(self):
        """More shared query terms → higher score."""
        provider = StubRerankerProvider()
        scores = asyncio.run(
            provider.rerank(
                "document retention policy",
                [
                    "the document retention policy requires annual review",
                    "our document retention schedule",  # 2 of 3 terms
                    "completely unrelated content here",
                ],
            )
        )
        assert scores[0] == 1.0
        assert 0.0 < scores[1] < 1.0
        assert scores[1] > scores[2] == 0.0

    def test_no_overlap_scores_zero(self):
        provider = StubRerankerProvider()
        scores = asyncio.run(provider.rerank("approval workflow", ["the weather is nice"]))
        assert scores == [0.0]

    def test_full_overlap_scores_one(self):
        provider = StubRerankerProvider()
        scores = asyncio.run(provider.rerank("approval workflow", ["this approval workflow document"]))
        assert scores == [1.0]

    def test_empty_documents(self):
        provider = StubRerankerProvider()
        assert asyncio.run(provider.rerank("query", [])) == []


@pytest.mark.unit
class TestNormalizeScores:

    def test_bounded_scores_clamped(self):
        """An already-[0,1] scale (Cohere) passes through with edge clamping."""
        assert normalize_scores([0.0, 0.5, 1.0]) == [0.0, 0.5, 1.0]

    def test_any_out_of_range_score_switches_to_sigmoid(self):
        """One out-of-range value ⇒ the provider's scale is unbounded — the
        whole set is squashed monotonically."""
        out = normalize_scores([-0.1, 0.0, 0.5, 1.0, 1.5])
        assert all(0.0 <= s <= 1.0 for s in out)
        assert out[0] < out[1] < out[2] < out[3] < out[4], "ordering preserved"

    def test_mixed_range_sigmoid_squashed(self):
        out = normalize_scores([0.0, 0.5, 1.0, 1.5, -0.1])
        assert all(0.0 <= s <= 1.0 for s in out)
        assert out[4] < out[0] < out[1] < out[2] < out[3], "ordering preserved"

    def test_unbounded_squashed_monotonically(self):
        raw = [-2.0, 0.0, 2.0]
        out = normalize_scores(raw)
        assert all(0.0 <= s <= 1.0 for s in out)
        assert out[0] < out[1] < out[2], "ordering preserved (monotonic transform)"

    def test_empty(self):
        assert normalize_scores([]) == []


@pytest.mark.unit
class TestCohereProviderGuards:

    def test_missing_sdk_raises_typed_error(self):
        """Without the cohere package installed the lazy client creation
        must raise the TYPED error (never an ImportError leak)."""
        provider = CohereRerankerProvider(api_key="k")
        try:
            import cohere  # noqa: F401
            pytest.skip("cohere installed — lazy-import failure path not exercisable")
        except ImportError:
            pass
        with pytest.raises(RerankerProviderError, match="cohere package is not installed"):
            asyncio.run(provider.rerank("q", ["doc"]))

    def test_blank_query_neutral_scores(self):
        """A blank query returns neutral scores without an API call."""
        provider = CohereRerankerProvider(api_key="k")
        scores = asyncio.run(provider.rerank("   ", ["a", "b"]))
        assert scores == [0.0, 0.0]


@pytest.mark.unit
class TestInitRerankerProvider:

    def _patch_settings(self, monkeypatch, **overrides):
        """Build a validation-free Settings via model_construct (skips env
        parsing and Literal checks — required for the unknown-provider case,
        where pydantic would otherwise reject the value before the init
        guard runs)."""
        from app.core.config import Settings

        defaults = dict(
            environment="development",
            log_level="INFO",
            jwt_secret_key="test-secret-key-for-testing-only-not-used-in-production",
            reranker_provider="none",
        )
        defaults.update(overrides)
        settings = Settings.model_construct(**defaults)
        monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
        return settings

    def test_none_disables_reranking(self, monkeypatch):
        self._patch_settings(monkeypatch, reranker_provider="none")
        result = init_reranker_provider()
        assert result is None
        assert get_reranker_provider() is None

    def test_stub_selected(self, monkeypatch):
        self._patch_settings(monkeypatch, reranker_provider="stub")
        result = init_reranker_provider()
        assert isinstance(result, StubRerankerProvider)
        assert get_reranker_provider() is result

    def test_cohere_without_key_raises(self, monkeypatch):
        self._patch_settings(monkeypatch, reranker_provider="cohere", cohere_api_key=None)
        with pytest.raises(ValueError, match="COHERE_API_KEY"):
            init_reranker_provider()

    def test_cohere_with_key_constructs(self, monkeypatch):
        self._patch_settings(
            monkeypatch, reranker_provider="cohere", cohere_api_key="test-key"
        )
        result = init_reranker_provider()
        assert isinstance(result, CohereRerankerProvider)
        assert result.model_name == "rerank-v3.5"

    def test_unknown_provider_raises(self, monkeypatch):
        self._patch_settings(monkeypatch, reranker_provider="bogus")
        with pytest.raises(ValueError, match="Unknown reranker_provider"):
            init_reranker_provider()

    def test_set_get_roundtrip(self):
        stub = StubRerankerProvider()
        set_reranker_provider(stub)
        assert get_reranker_provider() is stub
        set_reranker_provider(None)
        assert get_reranker_provider() is None
