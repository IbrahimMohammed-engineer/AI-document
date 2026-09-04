"""
ConflictRepository — all data access for conflicts + conflict_statements.

Tenant isolation: conflicts carries organization_id, so this repository
extends TenantScopedRepository and every query accepts organization_id
explicitly.

Dedup identity is CHUNK MEMBERSHIP, never topic-text similarity (§14):
  - find_by_exact_pair: any-status conflict already containing BOTH chunks.
  - find_open_by_single_chunk: OPEN conflict already containing one chunk
    (growth target).

Source authorization ("no existence leakage" — §18): ``list_for_org``
applies a single NOT EXISTS predicate excluding any conflict whose evidence
includes a statement backed by a private document the requesting user does
not own.  One SQL clause at list scale, not N per-conflict checks.

See PHASE-13-IMPLEMENTATION-PLAN.md §9, §14, §18, §23 Task 4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.conflict import Conflict, ConflictStatement
from app.models.document import Document
from app.repositories.base import TenantScopedRepository

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ConflictRepository(TenantScopedRepository[Conflict]):
    """All data access for the conflicts / conflict_statements tables."""

    model = Conflict

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        organization_id: str,
        topic: str,
        severity: str,
        detection_method: str,
    ) -> Conflict:
        """Insert a new OPEN conflict and flush (no commit — caller owns the tx)."""
        conflict = Conflict(
            organization_id=organization_id,
            topic=topic,
            severity=severity,
            status="OPEN",
            detection_method=detection_method,
        )
        return await self.add(conflict)

    async def add_statement(
        self,
        conflict_id: str,
        *,
        document_id: str,
        document_version_id: str,
        chunk_id: str,
        page_id: str,
        page_number: int,
        statement_text: str,
        section: str | None = None,
        effective_date=None,
    ) -> ConflictStatement:
        """Persist one evidence statement and flush.

        Raises IntegrityError on a duplicate (conflict_id, chunk_id) — the
        service layer catches this as "already added" (concurrent-insert
        backstop under uq_conflict_statements_conflict_chunk, §14).
        """
        statement = ConflictStatement(
            conflict_id=conflict_id,
            document_id=document_id,
            document_version_id=document_version_id,
            chunk_id=chunk_id,
            page_id=page_id,
            page_number=page_number,
            section=section,
            statement_text=statement_text,
            effective_date=effective_date,
        )
        self._session.add(statement)
        await self._session.flush()
        await self._session.refresh(statement)
        return statement

    # ── Dedup lookups (§14 — chunk-membership identity) ──────────────────────

    async def conflict_ids_for_chunks(
        self, organization_id: str, chunk_ids: list[str]
    ) -> dict[str, list[str]]:
        """Map each chunk ID to the conflict IDs it appears in (org-scoped).

        Single indexed query — drives both the exact-pair check (a chunk's
        conflicts list containing the other chunk's conflict) and the inline
        RAG surfacing query (§20).  Empty chunk list → {}.
        """
        if not chunk_ids:
            return {}
        stmt = (
            select(ConflictStatement.chunk_id, ConflictStatement.conflict_id)
            .join(Conflict, Conflict.id == ConflictStatement.conflict_id)
            .where(
                self._org_filter(organization_id),
                ConflictStatement.chunk_id.in_(chunk_ids),
            )
        )
        result = await self._session.execute(stmt)
        mapping: dict[str, list[str]] = {}
        for chunk_id, conflict_id in result.all():
            mapping.setdefault(chunk_id, []).append(conflict_id)
        return mapping

    async def find_by_exact_pair(
        self,
        organization_id: str,
        chunk_id_a: str,
        chunk_id_b: str,
    ) -> Optional[Conflict]:
        """Any conflict (ANY status) that already has statements for BOTH chunks.

        Matches regardless of OPEN/REVIEWED/DISMISSED — a resolved conflict is
        never re-persisted, never reopened (§14).
        """
        mapping = await self.conflict_ids_for_chunks(
            organization_id, [chunk_id_a, chunk_id_b]
        )
        conflicts_a = set(mapping.get(chunk_id_a, ()))
        conflicts_b = set(mapping.get(chunk_id_b, ()))
        shared = conflicts_a & conflicts_b
        if not shared:
            return None
        # Deterministic pick if data ever had multiple (should not happen —
        # a conflict's chunk membership is unique per chunk).
        conflict_id = sorted(shared)[0]
        return await self.get_by_id_for_org(conflict_id, organization_id)

    async def find_open_by_single_chunk(
        self, organization_id: str, chunk_id: str
    ) -> Optional[Conflict]:
        """An OPEN conflict that already has a statement for chunk_id.

        The growth target when a new candidate pair extends an existing
        open conflict (§14 — growth never touches REVIEWED/DISMISSED).
        """
        stmt = (
            select(Conflict)
            .join(ConflictStatement, ConflictStatement.conflict_id == Conflict.id)
            .where(
                self._org_filter(organization_id),
                ConflictStatement.chunk_id == chunk_id,
                Conflict.status == "OPEN",
            )
            .order_by(Conflict.detected_at)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Statements ────────────────────────────────────────────────────────────

    async def list_statements(self, conflict_id: str) -> list[ConflictStatement]:
        """All statements of a conflict, oldest first."""
        result = await self._session.execute(
            select(ConflictStatement)
            .where(ConflictStatement.conflict_id == conflict_id)
            .order_by(ConflictStatement.created_at)
        )
        return list(result.scalars().all())

    async def statement_count(self, conflict_id: str) -> int:
        result = await self._session.execute(
            select(func.count(ConflictStatement.id)).where(
                ConflictStatement.conflict_id == conflict_id
            )
        )
        return int(result.scalar_one())

    # ── List with source-document authorization (§18) ─────────────────────────

    async def list_for_org(
        self,
        organization_id: str,
        *,
        status: str | None = None,
        severity: str | None = None,
        requesting_user_id: str,
    ) -> list[Conflict]:
        """Conflicts the requesting user may see, newest detection first.

        Excludes (via one NOT EXISTS predicate, in-statement) any conflict
        with at least one statement backed by a document the user cannot
        read under the platform access-level rule: private documents are
        visible to their owner only (        organization/restricted remain
        org-readable — the same semantics resolve_allowed_documents applies).
        """
        stmt = select(Conflict).where(
            self._org_filter(organization_id),
            # No statement's source document may be a private document
            # owned by someone else.
            ~exists_private_unreadable(requesting_user_id),
        )
        if status is not None:
            stmt = stmt.where(Conflict.status == status)
        if severity is not None:
            stmt = stmt.where(Conflict.severity == severity)
        stmt = stmt.order_by(Conflict.detected_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_authorized_for_org(
        self,
        conflict_id: str,
        organization_id: str,
        requesting_user_id: str,
    ) -> Optional[Conflict]:
        """Fetch one conflict org-scoped + private-source-authorized, or None.

        Same predicate as list_for_org applied to a single row — a conflict
        backed by an unreadable private document is indistinguishable from a
        nonexistent one (404 either way, §27.4 no-leakage matrix).
        """
        stmt = select(Conflict).where(
            Conflict.id == str(conflict_id),
            self._org_filter(organization_id),
            ~exists_private_unreadable(requesting_user_id),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Resolution (§16 — flush only; caller commits + audits) ───────────────

    async def resolve(
        self,
        conflict: Conflict,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str | None,
    ) -> Conflict:
        """Apply the terminal OPEN → REVIEWED/DISMISSED transition."""
        conflict.status = status
        conflict.resolved_by = resolved_by
        conflict.resolved_at = _utc_now()
        conflict.resolution_note = resolution_note
        await self._session.flush()
        return conflict

    # ── Scan bookkeeping (§15) ────────────────────────────────────────────────

    async def has_in_flight_scan(self, organization_id: str) -> bool:
        """True when the org already has a PENDING/PROCESSING/RETRYING scan.

        The nightly trigger consults this before creating a new job so two
        concurrent scans for one org never exist (§15).
        """
        from app.models.processing_job import ProcessingJob

        stmt = (
            select(func.count(ProcessingJob.id))
            .where(
                # NOTE: ProcessingJob's org column, not Conflict's — this
                # query's FROM is processing_jobs, not the repository model.
                ProcessingJob.organization_id == organization_id,
                ProcessingJob.job_type == "CONFLICT_SCAN",
                ProcessingJob.status.in_(("PENDING", "PROCESSING", "RETRYING")),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one()) > 0


def exists_private_unreadable(requesting_user_id: str):
    """SQL NOT-EXISTS-negation predicate for private-source exclusion (§18).

    Returns a SQLAlchemy clause that is TRUE when NO statement of the outer
    ``conflicts`` row references a private document owned by someone other
    than ``requesting_user_id``.  Organization/restricted documents are
    org-readable by every member (the platform's existing access-level rule).
    """
    from sqlalchemy import exists

    private_doc = (
        select(ConflictStatement.id)
        .join(Document, Document.id == ConflictStatement.document_id)
        .where(
            ConflictStatement.conflict_id == Conflict.id,
            Document.access_level == "private",
            Document.owner_id != requesting_user_id,
        )
    )
    return exists(private_doc)
