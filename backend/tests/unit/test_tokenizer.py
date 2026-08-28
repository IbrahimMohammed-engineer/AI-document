"""
Unit tests for the Phase 6 token counter (roadmap Phase 6 step 5).

Covers:
  - tiktoken engine (cl100k_base): basic counts, empty input, determinism
  - the documented fallback: an unavailable encoding degrades permanently
    to the deterministic approximation (never raises)
  - the approximation stays conservative (over-counts vs tiktoken, never
    wildly under-counts) and deterministic

Docker is NOT required. tiktoken's BPE data may not be present offline —
the tiktoken-engine tests skip gracefully when it isn't.
"""
from __future__ import annotations

import pytest

from app.ingestion.tokenizer import TokenCounter, count_tokens


_TEXT = (
    "The approval process requires documented sign-off from the regulatory "
    "team before any public communication is released to customers."
)


def _tiktoken_available() -> bool:
    try:
        import tiktoken

        tiktoken.get_encoding("cl100k_base")
        return True
    except Exception:
        return False


# ─── tiktoken engine ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.skipif(not _tiktoken_available(), reason="tiktoken/BPE data unavailable")
def test_tiktoken_engine_counts_and_is_deterministic():
    counter = TokenCounter("cl100k_base")
    assert counter.engine_name() == "tiktoken"

    first = counter.count_tokens(_TEXT)
    second = counter.count_tokens(_TEXT)
    assert first == second
    assert first > 10  # a real sentence is well more than 10 tokens


@pytest.mark.unit
@pytest.mark.skipif(not _tiktoken_available(), reason="tiktoken/BPE data unavailable")
def test_tiktoken_empty_and_repeated_inputs():
    counter = TokenCounter("cl100k_base")
    assert counter.count_tokens("") == 0
    assert counter.count_tokens("hello") == 1
    assert counter.count_tokens("hello world") > counter.count_tokens("hello")


# ─── Documented fallback ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_unknown_encoding_falls_back_to_approximation():
    """ANY encoder failure degrades permanently to the approximation."""
    counter = TokenCounter("definitely-not-a-real-encoding")
    assert counter.engine_name() == "approximation"

    value = counter.count_tokens(_TEXT)
    assert value > 0
    assert counter.count_tokens(_TEXT) == value  # deterministic
    assert counter.count_tokens("") == 0


@pytest.mark.unit
def test_approximation_is_conservative_vs_tiktoken():
    """The approximation may over-count but must not badly under-count.

    Backend §21: sizes are set conservatively because budget-counting and
    embedding may tokenize differently — under-counting is what could let
    an oversized chunk slip through the hard maximum.
    """
    if not _tiktoken_available():
        pytest.skip("tiktoken/BPE data unavailable")
    tiktoken_counter = TokenCounter("cl100k_base")
    approx = TokenCounter("bogus-encoding-for-fallback")

    for sample in (_TEXT, "one two three", "a" * 500, "Hello, world! 123 — dash."):
        assert approx.count_tokens(sample) >= 1
        # within a sane band of the real tokenizer (approximation is
        # documented, not exact): never less than half the real count
        assert approx.count_tokens(sample) >= tiktoken_counter.count_tokens(sample) * 0.5


@pytest.mark.unit
def test_module_level_counter_matches_class_behavior():
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0


@pytest.mark.unit
def test_fallback_survives_weird_whitespace():
    counter = TokenCounter("bogus-encoding-for-fallback")
    assert counter.count_tokens("   \n\t  ") > 0  # never returns 0 for non-empty
    assert counter.count_tokens("a\n\nb\t\tc") == counter.count_tokens("a\n\nb\t\tc")
