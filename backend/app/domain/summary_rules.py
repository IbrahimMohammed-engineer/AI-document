"""
Summary sampling rules — the section-diverse long-document algorithm (Phase 14).

Pure, unit-testable, no I/O (mirrors comparison_rules.py / conflict_rules.py).

PHASE-14-IMPLEMENTATION-PLAN.md §5.6 — this deterministic algorithm is the
plan's own proposal, replacing the roadmap's under-specified "most-central
chunk via rag/retriever.py" prose (which would add cost/latency/
non-determinism for no proven benefit).  Implement it exactly as written:

  1. total_tokens = sum(chunk.token_count) over the version's full chunk set.
  2. total_tokens <= budget → ALL chunk ids, SamplingDisclosure(sampled=False,
     strategy="full").  The common case for ordinary-length policies/SOPs.
  3. Otherwise (long-document path):
     a. Top-level sections: parent_section_id is None, ordered by sort_order.
     b. Each top-level section's DESCENDANT CLOSURE is computed by walking
        the in-memory section list (a section belongs to T if it is T or its
        parent chain reaches T) — bounded, small per-document tree walk, no
        recursive SQL.
     c. Chunks "belonging" to T = chunks whose section_id is in T's closure,
        ordered by chunk_index; pick the MIDDLE `chunks_per_section` chunk(s)
        (median position) as the section's representative sample.
     d. Fallback for a top-level section with zero section_id-tagged chunks:
        use T.start_page/end_page to select chunks whose page_number falls in
        that range, same median-position rule.
     e. NO sections at all (entirely unstructured long document — the
        explicitly-supported "No structure detected" case): fall back to
        evenly-spaced chunks across the full chunk_index range, still
        disclosed as strategy="section_diverse" with included_section_ids=[].
     f. Return selected chunk ids in ORIGINAL READING ORDER (not by section).

Sampling is DISCLOSED, never silent (roadmap exit criterion 7): every
non-full selection produces SamplingDisclosure(sampled=True, ...), persisted
on document_summaries.sampling and rendered by the FE partial-state banner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class SectionRef:
    """Lightweight section equivalent — what the algorithm needs.

    Accepts ORM DocumentSection rows directly (duck-typed attribute access),
    so unit tests can pass simple stand-ins.
    """

    id: str
    parent_section_id: str | None
    sort_order: int
    start_page: int
    end_page: int | None


@dataclass
class ChunkRef:
    """Lightweight chunk equivalent — what the algorithm needs.

    ``page_number`` comes from the chunk's page relationship (resolved by
    the caller); ``section_id`` is the direct FK (may be None).
    """

    id: str
    section_id: str | None
    chunk_index: int
    token_count: int
    page_number: int


@dataclass
class SamplingDisclosure:
    """The persisted sampling disclosure object (document_summaries.sampling)."""

    sampled: bool
    strategy: str  # "full" | "section_diverse"
    included_section_ids: list[str] = field(default_factory=list)
    excluded_section_count: int = 0

    def to_json(self) -> dict:
        return {
            "sampled": self.sampled,
            "strategy": self.strategy,
            "included_section_ids": list(self.included_section_ids),
            "excluded_section_count": self.excluded_section_count,
        }


def select_representative_chunks(
    sections: Sequence[SectionRef],
    chunks: Sequence[ChunkRef],
    *,
    summary_context_token_budget: int,
    summary_sampling_chunks_per_section: int,
) -> tuple[list[str], SamplingDisclosure]:
    """The §5.6 section-diverse sampling algorithm.

    Args:
        sections: All DocumentSection rows for the version (any order; the
            algorithm orders top-level sections by sort_order itself).
        chunks: All DocumentChunk rows for the version, in reading order.
        summary_context_token_budget: Config budget (never hardcoded here —
            consumed as a parameter, matching comparison_rules' convention).
        summary_sampling_chunks_per_section: Chunks per top-level section.

    Returns:
        (selected_chunk_ids, disclosure) — chunk ids in original reading
        order; the disclosure describes what was selected and why.
    """
    ordered_chunks = sorted(chunks, key=lambda c: c.chunk_index)
    total_tokens = sum(c.token_count for c in ordered_chunks)

    if total_tokens <= summary_context_token_budget:
        # The common case — the whole document fits the budget.
        return (
            [c.id for c in ordered_chunks],
            SamplingDisclosure(sampled=False, strategy="full"),
        )

    # ── Long-document path ────────────────────────────────────────────────
    if not sections:
        # No structure detected at all (DB §15 / Backend §20 explicitly
        # supported) — evenly-spaced fallback, still disclosed.
        selected = _evenly_spaced(ordered_chunks, summary_context_token_budget)
        return (
            selected,
            SamplingDisclosure(
                sampled=True,
                strategy="section_diverse",
                included_section_ids=[],
                excluded_section_count=0,
            ),
        )

    top_level = sorted(
        (s for s in sections if s.parent_section_id is None),
        key=lambda s: s.sort_order,
    )

    # Precompute chunk membership per top-level section (descendant closure).
    closure_by_top: dict[str, set[str]] = {}
    for top in top_level:
        closure_by_top[top.id] = _descendant_closure(top, sections)

    selected_ids: list[str] = []
    included_section_ids: list[str] = []
    claimed_chunk_ids: set[str] = set()

    for top in top_level:
        closure = closure_by_top[top.id]
        members = [c for c in ordered_chunks if c.section_id in closure]
        picked = _pick_median_members(
            members, summary_sampling_chunks_per_section
        )
        if picked:
            included_section_ids.append(top.id)
            for chunk in picked:
                selected_ids.append(chunk.id)
                claimed_chunk_ids.add(chunk.id)
            continue

        # Fallback (§5.6 step d): zero section_id-tagged chunks — use the
        # section's page range instead.
        end_page = top.end_page if top.end_page is not None else top.start_page
        page_members = [
            c
            for c in ordered_chunks
            if top.start_page <= c.page_number <= end_page
            and c.id not in claimed_chunk_ids
        ]
        picked = _pick_median_members(
            page_members, summary_sampling_chunks_per_section
        )
        if picked:
            included_section_ids.append(top.id)
            for chunk in picked:
                selected_ids.append(chunk.id)
                claimed_chunk_ids.add(chunk.id)

    # Sections that contributed nothing (degenerate: empty page ranges too)
    excluded = len(top_level) - len(included_section_ids)

    # Any chunk not claimed by a section (preamble/untagged chunks) fills
    # remaining budget headroom so a long document is never summarized from
    # a coverage gap when untagged content exists.
    remaining_budget = summary_context_token_budget - sum(
        c.token_count for c in ordered_chunks if c.id in set(selected_ids)
    )
    if remaining_budget > 0:
        for chunk in ordered_chunks:
            if chunk.id in claimed_chunk_ids:
                continue
            if chunk.token_count <= remaining_budget:
                selected_ids.append(chunk.id)
                claimed_chunk_ids.add(chunk.id)
                remaining_budget -= chunk.token_count
            # Over-budget chunks are skipped (not truncated — the context
            # builder owns truncation policy).

    # Preserve original reading order (§5.6 step f) — never section order.
    selected_set = set(selected_ids)
    ordered_selection = [c.id for c in ordered_chunks if c.id in selected_set]

    return (
        ordered_selection,
        SamplingDisclosure(
            sampled=True,
            strategy="section_diverse",
            included_section_ids=included_section_ids,
            excluded_section_count=max(0, excluded),
        ),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _descendant_closure(top: SectionRef, sections: Sequence[SectionRef]) -> set[str]:
    """All section ids in ``top``'s subtree (including ``top`` itself).

    Bounded in-memory walk over the already-loaded section list — no
    recursive SQL needed (plan §5.6 step b).
    """
    children_by_parent: dict[str | None, list[SectionRef]] = {}
    for section in sections:
        children_by_parent.setdefault(section.parent_section_id, []).append(section)

    closure: set[str] = set()
    stack = [top]
    while stack:
        current = stack.pop()
        if current.id in closure:
            continue  # defensive against malformed cyclic data
        closure.add(current.id)
        for child in children_by_parent.get(current.id, []):
            stack.append(child)
    return closure


def _pick_median_members(
    members: Sequence[ChunkRef], count: int
) -> list[ChunkRef]:
    """The MIDDLE ``count`` chunk(s) of an ordered member list (median position).

    Members must already be in reading order.  An empty list yields [].
    """
    if not members or count <= 0:
        return []
    n = len(members)
    take = min(count, n)
    start = (n - take) // 2
    return list(members[start : start + take])


def _evenly_spaced(
    ordered_chunks: Sequence[ChunkRef], budget: int
) -> list[str]:
    """Evenly-spaced selection whose total tokens fit the budget (§5.6 step e).

    Walks the chunk list taking every Nth chunk; greedy pass keeps the
    selected set within the token budget while spreading coverage across the
    full chunk_index range.
    """
    total_tokens = sum(c.token_count for c in ordered_chunks)
    if not ordered_chunks or total_tokens <= 0:
        return [c.id for c in ordered_chunks[:1]]

    # Take roughly the fraction of chunks that fits the budget, then thin
    # greedily if rounding pushed the selection over.
    fraction = budget / total_tokens
    target_count = max(1, int(len(ordered_chunks) * fraction))
    if target_count >= len(ordered_chunks):
        return [c.id for c in ordered_chunks]

    step = len(ordered_chunks) / target_count
    picked: list[ChunkRef] = []
    used = 0
    for i in range(target_count):
        chunk = ordered_chunks[min(int(i * step), len(ordered_chunks) - 1)]
        if used + chunk.token_count > budget:
            continue
        picked.append(chunk)
        used += chunk.token_count

    if not picked:
        # Degenerate: individual chunks exceed the whole budget — take the
        # first chunk and let the context builder's truncation policy apply.
        picked = [ordered_chunks[0]]

    return [c.id for c in picked]
