"""
Unit tests — query rewriter (Phase 9, Backend §28).

Covers (roadmap Phase 9 §Testing): the trigger heuristic, the drift-guard
similarity floor fallback, and provider-failure degradation.  The core
invariant under test: the rewritten query is a retrieval-internal artifact
— on ANY fallback the user's original message is what retrieval receives.
"""
from __future__ import annotations

import pytest

from app.infrastructure.embeddings import (
    EmbeddingProvider,
    StubEmbeddingProvider,
)
from app.infrastructure.llm import (
    LLMMessage,
    LLMProviderError,
    StubLLMProvider,
)
from app.rag.query_rewriter import rewrite_query, should_rewrite


def _history(*turns: str) -> list[LLMMessage]:
    roles = ("user", "assistant")
    return [
        LLMMessage(role=roles[i % 2], content=content)
        for i, content in enumerate(turns)
    ]


class _SameVectorEmbedder(EmbeddingProvider):
    """Embeds everything identically → cosine 1.0 (drift guard passes)."""

    @property
    def model_name(self) -> str:
        return "same-vector"

    @property
    def dimensions(self) -> int:
        return 8

    async def embed(self, texts):
        return [[1.0] * 8 for _ in texts]


# ── Trigger heuristic ─────────────────────────────────────────────────────────

@pytest.mark.unit
class TestShouldRewrite:

    def test_first_message_never_rewrites(self):
        assert should_rewrite("approval?", history_length=0) is False

    def test_short_message_with_history_rewrites(self):
        assert should_rewrite("What about approval?", history_length=2) is True

    def test_long_selfcontained_message_skips(self):
        long_question = " ".join(["word"] * 30)
        assert should_rewrite(long_question, history_length=4) is False

    def test_context_markers_trigger_even_when_long(self):
        long_question = (
            "Given everything discussed so far, please explain in detail "
            "the full escalation procedure for it and list every responsible "
            "party mentioned earlier by name and role"
        )
        assert len(long_question.split()) > 12
        assert should_rewrite(long_question, history_length=2) is True


# ── Stage ─────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRewriteQuery:

    async def test_no_history_short_circuits_without_llm_call(self):
        provider = StubLLMProvider()
        outcome = await rewrite_query(
            "What is the approval process?", [], provider=provider
        )
        assert outcome.used_rewrite is False
        assert outcome.reason == "no-history"
        assert outcome.retrieval_query == "What is the approval process?"
        assert provider.call_count == 0  # heuristic pre-check saved the call

    async def test_selfcontained_long_message_skips_llm(self):
        provider = StubLLMProvider()
        long_question = " ".join(["word"] * 30)
        outcome = await rewrite_query(long_question, _history("hi"), provider=provider)
        assert outcome.reason == "self-contained"
        assert provider.call_count == 0

    async def test_successful_rewrite_used_for_retrieval(self):
        provider = StubLLMProvider(
            responder=lambda msgs: "What changes were made to the marketing approval process?"
        )
        outcome = await rewrite_query(
            "What about approval?",
            _history("What changed in the marketing policy?", "Several sections changed."),
            provider=provider,
            embedding_provider=_SameVectorEmbedder(),
        )
        assert outcome.used_rewrite is True
        assert outcome.reason == "rewritten"
        assert outcome.retrieval_query.startswith("What changes were made")

    async def test_drift_guard_falls_back_to_raw_message(self):
        # StubEmbeddingProvider: unrelated texts → unrelated vectors →
        # cosine ≈ 0 < floor → the rewrite is rejected.
        provider = StubLLMProvider(
            responder=lambda msgs: "completely unrelated invented topic about penguins"
        )
        original = "What about approval?"
        outcome = await rewrite_query(
            original,
            _history("prior turn"),
            provider=provider,
            embedding_provider=StubEmbeddingProvider(dimensions=64),
        )
        assert outcome.used_rewrite is False
        assert outcome.reason == "fallback-similarity-floor"
        assert outcome.retrieval_query == original
        assert outcome.rewritten_text is not None  # candidate recorded for observability

    async def test_llm_failure_falls_back_to_raw(self):
        class Failing(StubLLMProvider):
            async def generate(self, messages, **kwargs):
                raise LLMProviderError("down", code="LLM_SERVER_ERROR")

        outcome = await rewrite_query(
            "What about it?", _history("prior"), provider=Failing()
        )
        assert outcome.used_rewrite is False
        assert outcome.reason == "fallback-rewrite-error"
        assert outcome.retrieval_query == "What about it?"

    async def test_embedding_failure_falls_back_to_raw(self):
        class FailingEmbedder(EmbeddingProvider):
            model_name = "failing"
            dimensions = 8

            async def embed(self, texts):
                raise RuntimeError("embedding service down")

        provider = StubLLMProvider(responder=lambda msgs: "rewritten standalone question")
        outcome = await rewrite_query(
            "What about approval?",
            _history("prior"),
            provider=provider,
            embedding_provider=FailingEmbedder(),
        )
        assert outcome.used_rewrite is False
        assert outcome.reason == "fallback-embedding-error"
        assert outcome.retrieval_query == "What about approval?"

    async def test_identical_rewrite_counts_as_selfcontained(self):
        provider = StubLLMProvider(responder=lambda msgs: "  What about approval?  ")
        outcome = await rewrite_query(
            "What about approval?",
            _history("prior"),
            provider=provider,
            embedding_provider=_SameVectorEmbedder(),
        )
        assert outcome.used_rewrite is False
        assert outcome.reason == "self-contained"
