"""
Golden chunk-quality fixture corpus (roadmap Phase 6 step 10).

A small set of synthetic "golden documents" whose expected structure and
chunk properties were REVIEWED BY HAND — the regression baseline for all
future chunker/detector changes (roadmap Phase 6 step 10, exit criteria).
The four archetypes mirror the roadmap's required corpus:

  1. policy_with_deep_toc      — numbered headings nested 3 levels deep
  2. table_heavy_sop           — row/col-aware table content
  3. list_heavy_procedure      — a bulleted list that must stay intact
  4. unstructured_scan         — no detectable structure at all

Documents are expressed as (page_number, text) pairs — the same shape the
CHUNKING stage reads from `document_pages` — so the corpus feeds the pure
detector/chunker directly and stays independent of file formats. Unit tests
assert the golden expectations with a deterministic word-count token
counter, keeping them hermetic (no tiktoken/network).
"""
from __future__ import annotations

GoldenDocument = list[tuple[int, str]]


def policy_with_deep_toc() -> GoldenDocument:
    """Policy document with a 3-level numbered heading hierarchy."""
    return [
        (
            1,
            "\n".join(
                [
                    "Marketing Approval Policy",
                    "1. Purpose",
                    "This policy defines the marketing approval process for all public communications.",
                    "2. Scope",
                    "Applies to every department that publishes content on behalf of the organization.",
                    "3. Definitions",
                    "Campaign means a coordinated series of marketing activities supporting one objective.",
                ]
            ),
        ),
        (
            2,
            "\n".join(
                [
                    "4. Approval Process",
                    "All campaigns require documented approval before launch.",
                    "4.1 Marketing Review",
                    "The marketing director reviews creative assets for brand consistency.",
                    "4.2 Regulatory Review",
                    "The regulatory team verifies claims are substantiated by evidence.",
                    "4.2.1 Substantiation File",
                    "Every claim must cite a substantiation document in the claims repository.",
                ]
            ),
        ),
        (
            3,
            "\n".join(
                [
                    "5. Record Keeping",
                    "Approved campaign files are retained for seven years in the document system.",
                ]
            ),
        ),
    ]


def table_heavy_sop() -> GoldenDocument:
    """SOP whose core content is an inspection-frequency table (row/col-aware)."""
    return [
        (
            1,
            "\n".join(
                [
                    "1. Purpose",
                    "This procedure governs monthly safety equipment inspections.",
                    "2. Inspection Matrix",
                    "The matrix below defines inspection frequency by equipment class.",
                    "Equipment | Frequency | Owner | Escalation",
                    "Fire extinguishers | Monthly | Facilities | Week 1",
                    "Eyewash stations | Monthly | Labs | Week 1",
                    "Sprinkler system | Quarterly | Facilities | Week 2",
                    "3. Records",
                    "Completed checklists are filed in the safety repository within two days.",
                ]
            ),
        ),
    ]


def list_heavy_procedure() -> GoldenDocument:
    """Procedure whose core content is a bulleted list that must stay together."""
    return [
        (
            1,
            "\n".join(
                [
                    "1. Purpose",
                    "This procedure defines the weekly server backup process.",
                    "2. Required Materials",
                    "The following items are required before starting:",
                    "- Administrator credentials with elevated",
                    "- Offsite storage location verified",
                    "- Backup utility installed correctly",
                    "- Incident channel opened for escalation",
                    "3. Schedule",
                    "Backups run every Friday beginning at eleven at night.",
                ]
            ),
        ),
    ]


def unstructured_scan() -> GoldenDocument:
    """Scanned-memo archetype: plain sentences, no detectable structure."""
    return [
        (
            1,
            "\n".join(
                [
                    "The quarterly facilities review found the north entrance carpet worn and scheduled replacement.",
                    "Lighting in corridor two was replaced with efficient fixtures during the same visit.",
                ]
            ),
        ),
        (
            2,
            "\n".join(
                [
                    "Roof inspection reported no ponding and recommended clearing the east drains before autumn.",
                    "Vendor evaluations for the lobby renovation were collected and filed for the committee.",
                ]
            ),
        ),
    ]


# Deterministic token counter for hermetic unit tests (word count).
def word_counter(text: str) -> int:
    return len(text.split())
