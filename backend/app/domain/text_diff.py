"""
Text diffing for document comparison — pure domain, no I/O.

Implements the deterministic text comparison described in §9.5:
  1. Content-hash short-circuit (caller's responsibility — this module
     handles the "hashes differ" path only).
  2. Normalization: collapse whitespace, normalize line endings.
  3. Sentence-level diff using difflib.SequenceMatcher.
  4. UNCHANGED when normalized texts are identical (formatting-only absorbed).
  5. MODIFIED with span list + changed-token count for proportion computation.

See PHASE-12-IMPLEMENTATION-PLAN.md §9.5, §11 Task 2.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TextDiffResult:
    """Result of comparing two text sections.

    ``is_unchanged`` is True when the normalized texts are byte-for-byte
    identical after whitespace normalization — these sections produce no
    ``comparison_changes`` row.

    ``changed_token_count`` is the number of word-tokens that differ; used
    by ``compute_proportion_changed`` in ``comparison_rules.py``.

    ``diff_spans`` contains human-readable annotated spans for the UI
    (insert/delete/equal markers on sentence-token level).
    """
    is_unchanged: bool
    changed_token_count: int = 0
    total_token_count: int = 0  # max(old, new) word count
    diff_spans: list[DiffSpan] = field(default_factory=list)


@dataclass
class DiffSpan:
    """One annotated span from the sentence-level diff."""
    tag: str        # "equal" | "insert" | "delete" | "replace"
    old_text: str   # text from version A (empty for "insert")
    new_text: str   # text from version B (empty for "delete")


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Normalize text for comparison (§9.5 step 2).

    - Normalize line endings to \\n
    - Collapse runs of whitespace (spaces, tabs) within a line to a single space
    - Strip leading/trailing whitespace per line
    - Collapse blank lines to a single blank line
    - Strip leading/trailing whitespace from the whole text
    """
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip + collapse whitespace within each line
    lines = [" ".join(line.split()) for line in text.split("\n")]
    # Remove trailing blank lines, deduplicate consecutive blanks
    result_lines: list[str] = []
    for line in lines:
        if not line and result_lines and not result_lines[-1]:
            continue  # collapse consecutive blank lines
        result_lines.append(line)
    return "\n".join(result_lines).strip()


# ── Sentence tokenization ─────────────────────────────────────────────────────

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _sentence_tokens(text: str) -> list[str]:
    """Split normalized text into sentence-level tokens for diffing."""
    if not text:
        return []
    # Simple sentence splitter — good enough for policy/procedure text
    sentences = _SENTENCE_BOUNDARY.split(text)
    return [s.strip() for s in sentences if s.strip()]


# ── Public API ────────────────────────────────────────────────────────────────

def diff_text(old: str, new: str) -> TextDiffResult:
    """Compare two text sections and return a structured diff result.

    The function handles both the normalization step and the sentence-level
    diff.  Callers should NOT call this when content hashes match — use
    the hash short-circuit first (§9.5 step 1).

    Args:
        old: Section text from version A (raw, not pre-normalized).
        new: Section text from version B (raw, not pre-normalized).

    Returns:
        TextDiffResult with ``is_unchanged=True`` when normalized texts match,
        or ``is_unchanged=False`` with span list and changed-token counts.
    """
    norm_old = normalize_text(old)
    norm_new = normalize_text(new)

    # §9.5 step 4: if normalized texts are identical, absorb as formatting-only
    if norm_old == norm_new:
        return TextDiffResult(is_unchanged=True)

    # §9.5 step 3: sentence-level diff
    old_sentences = _sentence_tokens(norm_old)
    new_sentences = _sentence_tokens(norm_new)

    matcher = difflib.SequenceMatcher(
        None, old_sentences, new_sentences, autojunk=False
    )
    spans: list[DiffSpan] = []
    changed_tokens = 0
    total_old_words = _word_count(norm_old)
    total_new_words = _word_count(norm_new)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk = " ".join(old_sentences[i1:i2])
        new_chunk = " ".join(new_sentences[j1:j2])
        spans.append(DiffSpan(tag=tag, old_text=old_chunk, new_text=new_chunk))
        if tag == "equal":
            continue
        # §9.8 proportion_changed is a WORD-level changed-token ratio, so
        # within each non-equal sentence span we refine to a word-level
        # SequenceMatcher — a sentence-level count alone would charge BOTH
        # sides in full (e.g. one changed word in an 8-word sentence would
        # count as 16/8 = 1.0 instead of 2/8 = 0.25).
        if tag == "replace":
            old_words = old_chunk.split()
            new_words = new_chunk.split()
            word_matcher = difflib.SequenceMatcher(
                None, old_words, new_words, autojunk=False
            )
            for wtag, wi1, wi2, wj1, wj2 in word_matcher.get_opcodes():
                if wtag != "equal":
                    changed_tokens += (wi2 - wi1) + (wj2 - wj1)
        elif tag == "insert":
            changed_tokens += _word_count(new_chunk)
        elif tag == "delete":
            changed_tokens += _word_count(old_chunk)

    total_token_count = max(total_old_words, total_new_words)

    return TextDiffResult(
        is_unchanged=False,
        changed_token_count=changed_tokens,
        total_token_count=total_token_count,
        diff_spans=spans,
    )


def proportion_from_diff(result: TextDiffResult) -> float:
    """Compute the proportion-changed float from a TextDiffResult.

    Convenience wrapper so callers don't have to repeat the division.
    Returns 0.0 for unchanged results; 1.0 if total_token_count is 0.
    """
    if result.is_unchanged:
        return 0.0
    if result.total_token_count == 0:
        return 1.0
    return min(1.0, max(0.0, result.changed_token_count / result.total_token_count))


# ── Internal ──────────────────────────────────────────────────────────────────

def _word_count(text: str) -> int:
    """Simple whitespace-based word count."""
    return len(text.split()) if text.strip() else 0
