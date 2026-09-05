"""
Extraction rules — category constants, retrieval queries, bounds (Phase 14).

Pure constants + helpers, no I/O (mirrors comparison_rules.py /
conflict_rules.py).  The four categories and their synthesized retrieval
queries are the fixed V1 "standard_v1" schema set (plan §2.6 point 2 —
organization-configurable schemas are explicitly out of scope).
"""
from __future__ import annotations

# The four fixed extraction categories (DB CHECK constraint order).
CATEGORIES: tuple[str, ...] = ("requirement", "risk", "date", "party")

# The synthesized retrieval query per category (plan §5.9): targeted
# evidence-gathering, not general-purpose ranked search.
CATEGORY_QUERIES: dict[str, str] = {
    "requirement": "contractual requirements and obligations",
    "risk": "risks, liabilities, and penalties",
    "date": "dates, deadlines, and effective periods",
    "party": "parties, vendors, customers, and responsible roles",
}

# Named constants — actual values live in core/config.py (extraction_top_k_
# per_category / extraction_max_items_per_category) and are consumed as
# parameters by the service, matching comparison_rules.py's convention of
# referencing discoverable settings rather than hardcoding thresholds.
EXTRACTION_TOP_K_PER_CATEGORY = "extraction_top_k_per_category"
EXTRACTION_MAX_ITEMS_PER_CATEGORY = "extraction_max_items_per_category"

# The schema_key closed set (mirrors the DB CHECK constraint).
VALID_SCHEMA_KEYS: tuple[str, ...] = ("standard_v1",)
