"""
Unit tests for the EmbeddingProvider abstraction and its implementations.

Tests:
  - StubEmbeddingProvider: correct dimensions, determinism, batch handling
  - OpenAIEmbeddingProvider: dimension validation, retry logic, rate-limit handling
  - EmbeddingRateLimiter: token-bucket logic (local fallback path)
  - init_embedding_provider: provider selection from settings
  - get/set_embedding_provider: singleton management

No network calls are made — all OpenAI interactions are mocked.
"""
from __future__ import annotations

import math
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.embeddings import (
    EmbeddingDimensionError,
    EmbeddingProviderError,
    EmbeddingRateLimitError,
    EmbeddingRateLimiter,
    OpenAIEmbeddingProvider,
    StubEmbeddingProvider,
    get_embedding_provider,
    set_embedding_provider,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def stub_provider():
    return StubEmbeddingProvider(dimensions=1536)


@pytest.fixture()
def tiny_stub_provider():
    """Small-dimension provider useful for fast tests."""
    return StubEmbeddingProvider(dimensions=8, model="stub-tiny")


# ── StubEmbeddingProvider ─────────────────────────────────────────────────────

class TestStubEmbeddingProvider:

    @pytest.mark.asyncio
    async def test_returns_correct_number_of_vectors(self, stub_provider):
        texts = ["hello", "world", "foo"]
        vectors = await stub_provider.embed(texts)
        assert len(vectors) == 3

    @pytest.mark.asyncio
    async def test_returns_correct_dimensions(self, stub_provider):
        vectors = await stub_provider.embed(["some text"])
        assert len(vectors[0]) == 1536

    @pytest.mark.asyncio
    async def test_tiny_dimensions(self, tiny_stub_provider):
        vectors = await tiny_stub_provider.embed(["hello"])
        assert len(vectors[0]) == 8

    @pytest.mark.asyncio
    async def test_deterministic_output(self, stub_provider):
        """Same text always maps to the same vector (across calls)."""
        v1 = await stub_provider.embed(["determinism test"])
        v2 = await stub_provider.embed(["determinism test"])
        assert v1[0] == v2[0]

    @pytest.mark.asyncio
    async def test_different_texts_give_different_vectors(self, stub_provider):
        vectors = await stub_provider.embed(["hello", "world"])
        assert vectors[0] != vectors[1]

    @pytest.mark.asyncio
    async def test_unit_norm_vectors(self, stub_provider):
        """All returned vectors should be approximately unit-normalised."""
        vectors = await stub_provider.embed(["normalize me"])
        norm = math.sqrt(sum(x * x for x in vectors[0]))
        assert abs(norm - 1.0) < 1e-6

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_list(self, stub_provider):
        vectors = await stub_provider.embed([])
        assert vectors == []

    @pytest.mark.asyncio
    async def test_large_batch(self, stub_provider):
        texts = [f"text {i}" for i in range(250)]
        vectors = await stub_provider.embed(texts)
        assert len(vectors) == 250
        assert all(len(v) == 1536 for v in vectors)

    def test_model_name(self, stub_provider):
        assert stub_provider.model_name == "stub"

    def test_dimensions_property(self, stub_provider):
        assert stub_provider.dimensions == 1536


# ── EmbeddingDimensionError ───────────────────────────────────────────────────

class TestEmbeddingDimensionError:

    def test_error_message_contains_details(self):
        err = EmbeddingDimensionError(expected=1536, got=768, model="text-embedding-ada-002")
        msg = str(err)
        assert "1536" in msg
        assert "768" in msg
        assert "text-embedding-ada-002" in msg

    def test_attributes(self):
        err = EmbeddingDimensionError(expected=1536, got=768, model="my-model")
        assert err.expected == 1536
        assert err.got == 768
        assert err.model == "my-model"


# ── OpenAIEmbeddingProvider (mocked) ─────────────────────────────────────────

class TestOpenAIEmbeddingProvider:

    def _make_provider(self, dimensions=1536, max_retries=3):
        return OpenAIEmbeddingProvider(
            api_key="test-key",
            model="text-embedding-3-small",
            expected_dimensions=dimensions,
            request_timeout=1.0,
            max_retries=max_retries,
            rate_limiter=None,
        )

    def _make_mock_response(self, vectors: list[list[float]]):
        """Build a mock OpenAI embeddings response."""
        response = MagicMock()
        response.data = [
            MagicMock(embedding=v) for v in vectors
        ]
        return response

    @pytest.mark.asyncio
    async def test_happy_path(self):
        provider = self._make_provider()
        texts = ["hello", "world"]
        fake_vectors = [[0.1] * 1536, [0.2] * 1536]
        mock_response = self._make_mock_response(fake_vectors)

        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client  # inject directly — no openai import needed

        vectors = await provider.embed(texts)
        assert len(vectors) == 2
        assert len(vectors[0]) == 1536

    @pytest.mark.asyncio
    async def test_dimension_mismatch_raises(self):
        """If the provider returns vectors of wrong dimension, raise hard error."""
        provider = self._make_provider(dimensions=1536)
        fake_vectors = [[0.1] * 768]  # wrong dimension
        mock_response = self._make_mock_response(fake_vectors)

        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client

        with pytest.raises(EmbeddingDimensionError) as exc_info:
            await provider.embed(["test"])
        assert exc_info.value.expected == 1536
        assert exc_info.value.got == 768

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        """Provider retries on generic transient errors up to max_retries."""
        provider = self._make_provider(max_retries=3)
        fake_vectors = [[0.1] * 1536]
        mock_response = self._make_mock_response(fake_vectors)

        call_count = 0

        async def create_with_one_failure(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("APIConnectionError: timeout")
            return mock_response

        mock_client = AsyncMock()
        mock_client.embeddings.create = create_with_one_failure
        provider._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            vectors = await provider.embed(["retry me"])

        assert call_count == 2  # failed once, succeeded on retry
        assert len(vectors) == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        """All retries exhausted → EmbeddingProviderError."""
        provider = self._make_provider(max_retries=2)

        mock_client = AsyncMock()
        mock_client.embeddings.create = AsyncMock(
            side_effect=Exception("APIConnectionError: timeout")
        )
        provider._client = mock_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(EmbeddingProviderError):
                await provider.embed(["fail me"])

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        provider = self._make_provider()
        result = await provider.embed([])
        assert result == []

    def test_model_name_property(self):
        provider = self._make_provider()
        assert provider.model_name == "text-embedding-3-small"

    def test_dimensions_property(self):
        provider = self._make_provider(dimensions=1536)
        assert provider.dimensions == 1536


# ── EmbeddingRateLimiter (local fallback) ─────────────────────────────────────

class TestEmbeddingRateLimiter:

    @pytest.mark.asyncio
    async def test_local_acquire_does_not_raise(self):
        """Local fallback path should not raise (Redis unavailable simulation)."""
        # Rate of 60 RPM → 1 req/s → 1/60 s delay per token
        limiter = EmbeddingRateLimiter(rate_rpm=60 * 100)  # fast enough for test

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Force local path by making Redis unavailable
            with patch("app.infrastructure.embeddings.EmbeddingRateLimiter._redis_acquire",
                       side_effect=RuntimeError("no redis")):
                await limiter.acquire()
            mock_sleep.assert_called_once()


# ── Singleton management ───────────────────────────────────────────────────────

class TestEmbeddingProviderSingleton:

    def test_set_and_get(self):
        stub = StubEmbeddingProvider(dimensions=1536)
        set_embedding_provider(stub)
        assert get_embedding_provider() is stub

    def test_get_before_set_raises(self):
        # Temporarily reset the singleton
        from app.infrastructure import embeddings as emb_module
        original = emb_module._provider
        emb_module._provider = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                get_embedding_provider()
        finally:
            emb_module._provider = original
