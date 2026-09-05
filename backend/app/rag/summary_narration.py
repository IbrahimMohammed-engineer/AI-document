"""
Summary narration — deterministic re-render of persisted summary (Phase 14).

NO LLM call.  The persisted summary is already prose (validated, citation-
tagged); chat narration is a deterministic Markdown-ish rendering of the
stored content — the exact same text/citations that would render in the
Summary screen, just flattened into chat prose.  This is possible (and
cheaper/faster than comparison/conflict narration) specifically because
summary content is already prose; there is nothing left to "narrate"
(plan §2.6 point 6, §5.11).

The persisted ``summary`` JSONB payload (SummaryService.run) has the shape:
    {"executive_summary": [{"text", "citations": [...]}],   # 0..1 items
     "key_points": [{"text", "citations": [...]}], ...,
     "dates": [...], "roles": [...], "requirements": [...], "risks": [...],
     "topics": ["..."]}
"""
from __future__ import annotations

from typing import Any

_SECTION_HEADINGS = (
    ("key_points", "Key points", "No key points identified in this document."),
    ("dates", "Dates", "No dates identified in this document."),
    ("roles", "Roles", "No roles identified in this document."),
    ("requirements", "Requirements", "No requirements identified in this document."),
    ("risks", "Risks", "No risks identified in this document."),
)


def narrate_summary(summary: object) -> str:
    """Deterministic rendering of an already-persisted, already-validated summary.

    Every field is rendered — an explicitly empty field (persisted as an
    empty list by the pipeline, never an omitted key) renders its explicit
    empty-state line, driving the FE §6.12 "No dates identified in this
    document" pattern in chat as well (plan §5.7 step 9).
    """
    payload = getattr(summary, "summary", None)
    if not isinstance(payload, dict):
        return "The summary for this document is not available yet."

    lines: list[str] = []

    executive = payload.get("executive_summary") or []
    if isinstance(executive, list) and executive:
        first = executive[0]
        if isinstance(first, dict) and first.get("text"):
            lines.append(str(first["text"]).strip())
    else:
        lines.append("_The document produced no executive summary._")

    for key, heading, empty_line in _SECTION_HEADINGS:
        items = payload.get(key)
        rendered = _render_items(items)
        lines.append("")
        lines.append(f"**{heading}:**")
        if rendered:
            lines.extend(rendered)
        else:
            lines.append(f"_{empty_line}_")

    topics = payload.get("topics")
    topic_labels = [
        str(t).strip() for t in (topics if isinstance(topics, list) else []) if str(t).strip()
    ]
    if topic_labels:
        lines.append("")
        lines.append(f"**Topics:** {', '.join(topic_labels)}")

    return "\n".join(lines).strip()


def _render_items(items: Any) -> list[str]:
    """Render one field's item list as bullets (text with inline citation markers)."""
    if not isinstance(items, list):
        return []
    rendered: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        rendered.append(f"- {text}")
    return rendered
