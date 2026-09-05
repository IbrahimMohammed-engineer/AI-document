"""
Context builder — labeled, budgeted SOURCE blocks (Phase 9).

Backend §33: constructs the structured, labeled prompt context from the
reranked chunk set — never a raw concatenation of chunk text.

Responsibilities:
  - DEDUPLICATION: near-duplicate chunks (exact-after-normalization or one
    contained in another — e.g. overlapping neighbours from the chunking
    overlap strategy that both survived reranking) collapse to the
    higher-scored one; redundant context wastes budget and biases the LLM.
  - ORDERING: sources are ordered by relevance score (highest first) —
    LLMs weight earlier context more heavily, so the strongest evidence
    gets the positional advantage.
  - BUDGET: a fixed token budget (4,000–6,000 documented band; config
    default 5,000) is enforced — sources are added in relevance order
    until the budget is reached; the LLM call is never made with an
    unbounded context.
  - LABELING: every SOURCE block carries exactly the metadata a citation
    needs (document name, page, section).  The chunk id is NEVER shown in
    prompt text — it lives in the parallel ``source_index → chunk_id`` map
    the citation stage (Phase 10) resolves through, so citation data is
    trustworthy by construction rather than dependent on the LLM correctly
    reproducing names/numbers (Backend §33/§35).
  - METADATA PRESERVATION: every field surfaced in a SOURCE block
    round-trips into future citation rows unchanged — carried through from
    this assembly step, never re-derived after generation (Backend §33).

SECURITY (Backend §53 items 2–3): the SOURCE delimiter format defined here
is never reused for any other content in any prompt.  This module and
``rag/generator.py`` are the ONLY prompt-assembling code.

See:
  Backend-Architecture-Documentation.md §33 (Context Assembly)
  Backend-Architecture-Documentation.md §53 (Prompt Injection Protection)
  roadmap Phase 9 step 4
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.ingestion.tokenizer import TokenCounter
from app.rag.retriever import SearchResult

logger = logging.getLogger(__name__)

_counter = TokenCounter()


def _count_tokens(text: str) -> int:
    return _counter.count_tokens(text)


def _normalize(text: str) -> str:
    """Normalization for near-duplicate detection (case + whitespace)."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


# ── Delimiter-injection defense (Phase 16 plan §3.3) ──────────────────────────
#
# The SOURCE-block delimiters below are RESERVED control tokens: chunk text
# that begins a line with any of them could forge a new SOURCE header, a new
# triple-quote block boundary, or fake metadata in the prompt. Matching lines
# are prefixed with a visible warning marker rather than silently dropped —
# evidence is preserved (audit traceability) but can no longer hijack the
# delimiter structure. Anchored with re.match on the LSTRIPPED line, so
# content that merely CONTAINS the words "source" or "document" mid-line is
# untouched (Phase 16 plan §19 checklist).
_CONTROL_LINE_RE = re.compile(
    r'^(SOURCE\s+\d|Document:|Page:|Section:|""")',
    re.IGNORECASE,
)

_CONTROL_LINE_MARKER = "WARNING[REDACTED CONTROL TOKEN] "


def _sanitize_content(content: str) -> str:
    """Redact control-format lines from untrusted chunk content.

    Prevents delimiter-injection attacks where a malicious document embeds
    fake SOURCE headers / triple-quote boundaries to forge citations or
    hijack the instruction channel. Only lines that BEGIN with a reserved
    control token are redacted; ordinary prose is never modified.
    """
    lines = content.splitlines(keepends=True)
    sanitized = [
        (_CONTROL_LINE_MARKER + line) if _CONTROL_LINE_RE.match(line.lstrip()) else line
        for line in lines
    ]
    return "".join(sanitized)


def _sanitize_metadata(value: str | None) -> str:
    """Collapse newlines in header metadata (document/section names).

    Metadata is rendered on a single header line inside the SOURCE block —
    embedded newlines would let a forged name start a new control line
    (e.g. a fake ``SOURCE 99`` header). Collapsing them keeps every
    metadata value structurally inert.
    """
    return re.sub(r"[\r\n]+", " ", value or "")


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class SourceBlock:
    """One labeled source — everything a citation (Phase 10) needs.

    ``index`` is the 1-based SOURCE label the LLM is asked to reference.
    Every metadata field is carried through unchanged from the retrieval
    result — never re-derived (Backend §33 metadata preservation).
    """

    index: int                       # 1-based SOURCE N label
    chunk_id: str                    # backend-owned — NEVER in prompt text
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str                     # citation page anchor (Phase 10)
    page_number: int
    section_title: str | None
    content: str
    relevance: float
    token_count: int                 # tokens of the FORMATTED block

    def format(self) -> str:
        """Render the block.  The delimiter format is unique to SOURCE
        blocks — never reused elsewhere in any prompt (Backend §53).
        Metadata is newline-collapsed and content is sanitized so a forged
        document can never break out of the evidence envelope (Phase 16)."""
        lines = [
            f"SOURCE {self.index}",
            f"Document: {_sanitize_metadata(self.document_name)}",
            f"Page: {self.page_number}",
        ]
        if self.section_title:
            lines.append(f"Section: {_sanitize_metadata(self.section_title)}")
        lines.append('"""')
        lines.append(self.content)
        lines.append('"""')
        return "\n".join(lines)


@dataclass
class ContextBundle:
    """The assembled context plus the backend-owned resolution map."""

    blocks: list[SourceBlock] = field(default_factory=list)
    prompt_text: str = ""            # formatted SOURCE blocks (joined)
    total_tokens: int = 0
    deduped_count: int = 0           # dropped as near-duplicates
    dropped_by_budget: int = 0       # didn't fit the budget

    @property
    def source_index(self) -> dict[int, str]:
        """The parallel ``SOURCE N → chunk_id`` map (Backend-owned).

        Citation resolution NEVER depends on the LLM's memory of document
        names or page numbers — the backend resolves labels through this
        map (Backend §33/§35).
        """
        return {b.index: b.chunk_id for b in self.blocks}


# ── Deduplication ─────────────────────────────────────────────────────────────

def dedup_sources(results: list[SearchResult]) -> tuple[list[SearchResult], int]:
    """Drop near-duplicate/contained chunks, keeping the higher-scored one.

    A chunk is a duplicate when its normalized content equals another's OR
    is fully contained within a longer one (chunking overlap can make both
    survive reranking).  Input may be in any order — the higher-``relevance``
    chunk always wins; stable input order breaks exact ties.
    """
    kept: list[SearchResult] = []
    deduped = 0
    for candidate in sorted(
        results, key=lambda r: r.relevance, reverse=True
    ):
        norm = _normalize(candidate.content)
        if not norm:
            # Empty/whitespace content is a duplicate of nothing useful —
            # keep it out of the prompt entirely.
            deduped += 1
            continue
        is_dup = False
        for existing in kept:
            existing_norm = _normalize(existing.content)
            # Either direction: the candidate may be the shorter contained
            # one OR the longer container of an already-kept chunk.
            if norm == existing_norm or norm in existing_norm or existing_norm in norm:
                is_dup = True
                break
        if is_dup:
            deduped += 1
            continue
        kept.append(candidate)
    return kept, deduped


# ── Assembly ──────────────────────────────────────────────────────────────────

def build_context(
    results: list[SearchResult],
    *,
    budget_tokens: int | None = None,
) -> ContextBundle:
    """Assemble the labeled SOURCE-block context within the token budget.

    Args:
        results: Reranked retrieval results (any order; re-sorted here).
        budget_tokens: Override; defaults to ``settings.context_token_budget``
            (the documented 4,000–6,000 band, Backend §33).

    Returns:
        ContextBundle — blocks carry 1-based indices in relevance order;
        ``prompt_text`` is the joined SOURCE section ready for the
        generator to embed in the user message.
    """
    if budget_tokens is None:
        budget_tokens = get_settings().context_token_budget

    deduped, deduped_count = dedup_sources(results)
    ranked = sorted(deduped, key=lambda r: r.relevance, reverse=True)

    bundle = ContextBundle(deduped_count=deduped_count)
    formatted_blocks: list[str] = []
    used = 0

    for result in ranked:
        block = SourceBlock(
            index=len(bundle.blocks) + 1,
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            document_version_id=result.document_version_id,
            document_name=result.document_name,
            page_id=result.page_id,
            page_number=result.page_number,
            section_title=result.section_title,
            # SECURITY (Phase 16): untrusted chunk content is sanitized
            # BEFORE it can touch any SOURCE-block delimiter.
            content=_sanitize_content(result.content),
            relevance=result.relevance,
            token_count=0,
        )
        text = block.format()
        cost = _count_tokens(text)

        if used + cost <= budget_tokens:
            block.token_count = cost
            used += cost
            bundle.blocks.append(block)
            formatted_blocks.append(text)
            continue

        if not bundle.blocks:
            # A single chunk larger than the whole budget: truncate its
            # content to fit rather than answer with zero context.
            # NOTE: block.content is already sanitized — truncation never
            # re-introduces unsanitized text (Phase 16).
            overhead = _count_tokens(text[: text.find('"""')]) + 10
            allowed = max(1, budget_tokens - overhead)
            truncated = _truncate_to_tokens(block.content, allowed)
            block.content = truncated + "\n…[truncated]"
            text = block.format()
            block.token_count = _count_tokens(text)
            used += block.token_count
            bundle.blocks.append(block)
            formatted_blocks.append(text)
            logger.warning(
                "Context builder: first chunk exceeded the budget (%d > %d) — truncated",
                cost, budget_tokens,
            )

        bundle.dropped_by_budget += 1
        if bundle.dropped_by_budget == 1:
            logger.info(
                "Context builder: budget reached (%d tokens) — dropping remaining sources",
                used,
            )

    bundle.prompt_text = "\n\n".join(formatted_blocks)
    bundle.total_tokens = used

    logger.info(
        "Context builder: %d sources (%d deduped, %d budget-dropped), %d/%d tokens",
        len(bundle.blocks), bundle.deduped_count,
        bundle.dropped_by_budget, bundle.total_tokens, budget_tokens,
    )
    return bundle


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Word-boundary truncation to approximately max_tokens."""
    if max_tokens <= 0:
        return ""
    words = text.split()
    ratio = max(1.0, _count_tokens(text) / max(len(words), 1))
    keep = max(1, int(max_tokens / ratio))
    return " ".join(words[:keep])
