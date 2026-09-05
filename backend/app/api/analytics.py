"""
Minimal analytics API router (Phase 14 groundwork, plan §6.7 / §2.6 point 8).

The roadmap scopes Phase 14 as "begins accumulating" metrics and defers the
full metrics platform to Phase 19 — so this router ships exactly ONE
read-only endpoint over already-existing columns
(``messages.groundedness``, per-message ``citations`` counts, ``documents``
count).  No charts, no time-range selector, no cost breakdown, no CSV
export — that is explicitly Phase 15/19 scope.

Access: ``analytics:read`` permission (seeded by migration 002; the FE
/analytics route already exists as a placeholder gated on the same key).
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession, require_permission
from app.models.document import Document
from app.models.message import Citation, Message

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
)

logger = logging.getLogger(__name__)


class AnalyticsSummaryResponse(BaseModel):
    """The four Phase-14 KPI numbers (FE §6.7 KpiCard row)."""

    documentCount: int
    questionCount: int
    groundedAnswerPct: float
    citationCoveragePct: float


@router.get(
    "/summary",
    response_model=AnalyticsSummaryResponse,
    summary="Minimal Phase-14 analytics KPI snapshot",
    description=(
        "Read-only aggregates over existing columns:\n"
        "- `documentCount`: active documents in the organization.\n"
        "- `questionCount`: USER messages (questions asked).\n"
        "- `groundedAnswerPct`: share of assistant answers whose groundedness "
        "is 'grounded' or 'partial' (an answer was produced).\n"
        "- `citationCoveragePct`: share of assistant answers carrying at "
        "least one resolved citation.\n\n"
        "Requires the `analytics:read` permission."
    ),
)
async def get_analytics_summary(
    db: DbSession,
    user: Annotated[object, Depends(require_permission("analytics:read"))],
) -> AnalyticsSummaryResponse:
    organization_id = user.organization_id  # type: ignore[attr-defined]

    documents_result = await db.execute(
        select(func.count(Document.id)).where(
            Document.organization_id == organization_id,
            Document.deleted_at.is_(None),
            Document.status == "active",
        )
    )
    document_count = int(documents_result.scalar_one())

    question_result = await db.execute(
        select(func.count(Message.id)).where(
            Message.conversation_id.in_(
                select(_conversation_table().id).where(
                    _conversation_table().organization_id == organization_id,
                    _conversation_table().deleted_at.is_(None),
                )
            ),
            Message.role == "USER",
        )
    )
    question_count = int(question_result.scalar_one())

    assistant_metrics = await db.execute(
        select(
            Message.groundedness,
            func.count(Message.id),
        )
        .where(
            Message.role == "ASSISTANT",
            Message.conversation_id.in_(
                select(_conversation_table().id).where(
                    _conversation_table().organization_id == organization_id,
                    _conversation_table().deleted_at.is_(None),
                )
            ),
        )
        .group_by(Message.groundedness)
    )
    groundedness_counts = {
        (groundedness or "none"): int(count)
        for groundedness, count in assistant_metrics.all()
    }
    answer_total = sum(groundedness_counts.values())
    answered = groundedness_counts.get("grounded", 0) + groundedness_counts.get(
        "partial", 0
    )
    grounded_answer_pct = (answered / answer_total * 100.0) if answer_total else 0.0

    # Citation coverage: assistant answers with >= 1 resolved citation.
    with_citations_result = await db.execute(
        select(func.count(func.distinct(Citation.message_id))).where(
            Citation.message_id.in_(
                select(Message.id).where(
                    Message.role == "ASSISTANT",
                    Message.conversation_id.in_(
                        select(_conversation_table().id).where(
                            _conversation_table().organization_id == organization_id,
                            _conversation_table().deleted_at.is_(None),
                        )
                    ),
                )
            )
        )
    )
    answers_with_citations = int(with_citations_result.scalar_one())
    citation_coverage_pct = (
        (answers_with_citations / answer_total * 100.0) if answer_total else 0.0
    )

    logger.info(
        "GET /analytics/summary: org=%s docs=%d questions=%d answers=%d user=%s",
        organization_id, document_count, question_count, answer_total,
        user.id,  # type: ignore[attr-defined]
    )
    return AnalyticsSummaryResponse(
        documentCount=document_count,
        questionCount=question_count,
        groundedAnswerPct=round(grounded_answer_pct, 1),
        citationCoveragePct=round(citation_coverage_pct, 1),
    )


def _conversation_table():
    from app.models.conversation import Conversation

    return Conversation
