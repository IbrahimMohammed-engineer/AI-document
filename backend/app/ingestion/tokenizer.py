"""
Token counting for chunk budgeting (roadmap Phase 6 step 5; Backend §21).

The chunker sizes retrieval units in TOKENS, counted with the tokenizer
matching the platform's default LLM family (tiktoken `cl100k_base` —
Backend §21). A documented approximation is acceptable: token *counting*
for budget purposes and *embedding* are governed by different models with
potentially different tokenization, so the chunker's target/max sizes are
set conservatively enough to absorb small counting discrepancies.

Degradation contract: when tiktoken is unavailable (not installed, or its
BPE data cannot be fetched in an offline environment) the module PERMANENTLY
falls back — for the lifetime of the process — to a deterministic
word-boundary approximation and logs once. Counting stays deterministic
within a process either way, which is what the chunker's invariants
(hard-max enforcement, overlap bounds) actually require.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Word-ish tokens for the approximation: whitespace-separated words plus
# trailing punctuation split-off. ~0.75 words/token is the commonly cited
# English ratio for cl100k-style BPEs; we keep 4 chars/token as the floor.
_WORD_RE = re.compile(r"\S+")


class TokenCounter:
    """Deterministic token counter with tiktoken + documented fallback.

    One instance per process (module-level `counter`); constructing encoders
    is expensive and the fallback decision must not flip mid-run.
    """

    def __init__(self, encoding_name: str | None = None) -> None:
        self._encoding_name = encoding_name
        self._encoder: Any = None  # tiktoken.Encoding when the engine is active
        self._engine: str = "uninitialized"
        self._fallback_warned = False

    # ── Public API ────────────────────────────────────────────────────────────

    def count_tokens(self, text: str) -> int:
        """Count tokens in `text` (never raises, never returns negatives)."""
        if not text:
            return 0
        engine = self._resolve_engine()
        if engine == "tiktoken":
            return len(self._encoder.encode(text))
        return self._approximate(text)

    def engine_name(self) -> str:
        """'tiktoken' or 'approximation' — recorded in chunk stage logs."""
        self._resolve_engine()
        return self._engine

    # ── Internals ─────────────────────────────────────────────────────────────

    def _resolve_engine(self) -> str:
        """Lazily build the encoder; ANY failure degrades permanently."""
        if self._engine != "uninitialized":
            return self._engine
        encoding = self._encoding_name or get_settings().tokenizer_encoding
        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding(encoding)
            self._engine = "tiktoken"
        except Exception as exc:  # ImportError OR offline BPE fetch failure
            self._engine = "approximation"
            if not self._fallback_warned:
                self._fallback_warned = True
                logger.warning(
                    "tiktoken encoding %r unavailable (%s) — using the "
                    "documented word-boundary approximation for chunk token "
                    "counts (Backend §21)",
                    encoding,
                    exc,
                )
        return self._engine

    @staticmethod
    def _approximate(text: str) -> int:
        """Deterministic word-boundary approximation (documented, Backend §21).

        max(words / 0.75, chars / 4), rounded up — conservative (over-counts
        slightly) so hard-max enforcement never under-splits.
        """
        words = len(_WORD_RE.findall(text))
        chars = len(text)
        return max(1, math.ceil(max(words / 0.75, chars / 4)))


counter = TokenCounter()


def count_tokens(text: str) -> int:
    """Module-level convenience wrapper over the process-wide counter."""
    return counter.count_tokens(text)
