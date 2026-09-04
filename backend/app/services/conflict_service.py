"""
ConflictService — the single Phase 13 domain service (static methods, explicit
session, mirroring ComparisonService's convention).

Responsibilities (PHASE-13-IMPLEMENTATION-PLAN.md §10–§20):
  - generate_candidates():        org-scoped cross-document candidate pairs
                                  via pgvector (chunk's own embedding as the
                                  query vector — no re-embedding), CURRENT
                                  versions only (§10, §12).
  - check_contradiction():        the bounded, structured LLM call deciding
                                  is_conflict/confidence — GATES persistence
                                  only; the LLM never writes to the database,
                                  never decides severity, effective-date
                                  validity, or deduplication (§11).
  - persist_or_merge():           the single deterministic write path both
                                  triggers call; dedup identity is CHUNK
                                  MEMBERSHIP (exact-pair → skip; single-chunk
                                  OPEN match → grow; collision → log+skip;
                                  otherwise create) (§14).
  - classify_conflict_priority(): live ACTIVE/LIKELY_RESOLVED classification
                                  via Phase 12's classify_version_state —
                                  never persisted (§12).
  - seed_from_comparison():       comparison-derived seeding — MODIFIED +
                                  MAJOR + both sides CURRENT, no LLM call
                                  (§13).
  - run_scan():                   the checkpointed, resumable org-wide scan
                                  loop driven by the CONFLICT_SCAN worker
                                  handler (§15).
  - resolve():                    the audited, role-gated OPEN →
                                  REVIEWED/DISMISSED terminal transition
                                  (§16–§17).
  - list/get serialization:       live priority + per-statement version_state
                                  resolution for the API layer (§18).
  - list_for_chat():              authorized OPEN conflicts for the
                                  CONFLICT_DETECTION chat intent (§19).
  - find_conflicts_among_chunks():deterministic inline-surfacing query —
                                  >=2 retrieved chunks that are statements of
                                  the same OPEN conflict (§20).

See Backend-Architecture-Documentation.md §42; Database §26.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.conflict_rules import (
    CANDIDATE_SIMILARITY_THRESHOLD,
    CANDIDATE_TOP_K,
    CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD,
    CONTRADICTION_CHECK_TOKEN_BUDGET,
    classify_conflict_severity,
    is_critical_section,
)
from app.domain.permissions import PermissionKey
from app.domain.versioning import classify_version_state
from app.infrastructure.llm import LLMMessage, LLMProvider, LLMProviderError
from app.models.conflict import Conflict, ConflictStatement
from app.models.document import Document, DocumentVersion
from app.models.organization import Organization
from app.models.user import User
from app.rag.conflict_parsing import ContradictionResult, parse_contradiction_check
from app.rag.prompts import CONTRADICTION_CHECK_SYSTEM_PROMPT, CONTRADICTION_CHECK_USER_TEMPLATE
from app.repositories.conflict_repository import ConflictRepository
from app.services.audit_logger import AuditAction, AuditLogger
from app.services.authorization_service import AuthorizationService

logger = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class StatementChunk:
    """Minimal provenance + content for one side of a candidate pair.

    Normalized from either a candidate-generation projection dict (side A)
    or a ChunkSearchResult (side B) — everything persist_or_merge needs to
    write a ConflictStatement row.
    """

    chunk_id: str
    document_id: str
    document_version_id: str
    page_id: str
    page_number: int
    section_title: str | None = None
    section_number: str | None = None
    content: str = ""


@dataclass
class CandidatePair:
    """One cross-document chunk pair promoted to contradiction checking."""

    chunk_a: StatementChunk
    chunk_b: StatementChunk
    similarity: float


@dataclass
class ConflictNotice:
    """Deterministic inline-surfacing payload (§20) — rendered by the FE from
    the structured conflict_notice event, never parsed from generated text."""

    conflict_id: str
    topic: str
    severity: str


def _statement_chunk(source: Any) -> StatementChunk:
    """Normalize a projection dict / ChunkSearchResult / ORM-ish object.

    Two dict shapes are accepted:
      - candidate-generation projection: chunk_id / document_id / ... /
        section_title / section_number / content
      - ComparisonService.resolve_chunk_provenance: document_id / ... /
        section / content (keyed by chunk_id externally — callers add
        ``chunk_id`` before normalizing)
    """
    if isinstance(source, StatementChunk):
        return source
    if isinstance(source, dict):
        title = source.get("section_title") or source.get("section")
        number = source.get("section_number")
        return StatementChunk(
            chunk_id=str(source["chunk_id"]),
            document_id=str(source["document_id"]),
            document_version_id=str(source["document_version_id"]),
            page_id=str(source["page_id"]),
            page_number=int(source["page_number"]),
            section_title=title,
            section_number=number,
            content=str(source.get("content") or ""),
        )
    # ChunkSearchResult (or any attribute-shaped source)
    return StatementChunk(
        chunk_id=str(getattr(source, "chunk_id")),
        document_id=str(getattr(source, "document_id")),
        document_version_id=str(getattr(source, "document_version_id")),
        page_id=str(getattr(source, "page_id")),
        page_number=int(getattr(source, "page_number")),
        section_title=getattr(source, "section_title", None),
        section_number=None,
        content=str(getattr(source, "content", "") or ""),
    )


def _section_label(chunk: StatementChunk) -> str | None:
    if chunk.section_number and chunk.section_title:
        return f"{chunk.section_number} {chunk.section_title}"
    return chunk.section_number or chunk.section_title


# ── Service ───────────────────────────────────────────────────────────────────

class ConflictService:
    """All conflict-detection business logic (static methods, explicit session)."""

    # ── §10: candidate generation ─────────────────────────────────────────────

    @staticmethod
    async def resolve_current_versions(
        organization_id: str, db: AsyncSession
    ) -> tuple[dict[str, DocumentVersion], dict[str, DocumentVersion]]:
        """Resolve each active document's CURRENT version (org-scoped).

        Returns:
            (current_by_document_id, version_by_version_id) — the second map
            carries every READY version object seen (effective_date lookups,
            version-id → document joins) so callers avoid re-querying.
        """
        from app.domain.versioning import resolve_current_version

        docs_result = await db.execute(
            select(Document).where(
                Document.organization_id == organization_id,
                Document.deleted_at.is_(None),
                Document.status == "active",
            )
        )
        documents = list(docs_result.scalars().all())

        versions_result = await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id.in_([d.id for d in documents]),
                DocumentVersion.status == "READY",
            )
        ) if documents else None
        all_versions = list(versions_result.scalars().all()) if versions_result else []

        by_doc: dict[str, list[DocumentVersion]] = {}
        version_by_id: dict[str, DocumentVersion] = {}
        for version in all_versions:
            by_doc.setdefault(version.document_id, []).append(version)
            version_by_id[version.id] = version

        current_by_document: dict[str, DocumentVersion] = {}
        for document in documents:
            current = resolve_current_version(by_doc.get(document.id, []))
            if current is not None:
                current_by_document[document.id] = current  # type: ignore[assignment]
        return current_by_document, version_by_id

    @staticmethod
    async def generate_candidates(
        organization_id: str, db: AsyncSession
    ) -> AsyncIterator[CandidatePair]:
        """Yield cross-document candidate pairs (org-scoped, CURRENT-only).

        Deterministic filter chain (§10): different document only, similarity
        >= CANDIDATE_SIMILARITY_THRESHOLD, top CANDIDATE_TOP_K per chunk, and
        the pair-ordering rule (chunk_b.id > chunk_a.id) so every unordered
        pair is generated at most once per call.
        """
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        chunk_repo = DocumentChunkRepository(db)
        current_by_document, _ = await ConflictService.resolve_current_versions(
            organization_id, db
        )
        all_current_version_ids = [v.id for v in current_by_document.values()]
        if not all_current_version_ids:
            return

        for document_id in sorted(current_by_document):
            current_version = current_by_document[document_id]
            refs = await chunk_repo.list_embedded_chunk_refs_for_version(current_version.id)
            for ref in refs:
                ref["document_id"] = document_id
                chunk_a = _statement_chunk(ref)
                if chunk_a.chunk_id is None:
                    continue
                async for pair in ConflictService.generate_candidates_for_chunk(
                    chunk_a,
                    organization_id=organization_id,
                    version_ids=all_current_version_ids,
                    chunk_repo=chunk_repo,
                    db=db,
                ):
                    yield pair

    @staticmethod
    async def generate_candidates_for_chunk(
        chunk_a: StatementChunk,
        *,
        organization_id: str,
        version_ids: list[str],
        chunk_repo: Any,
        db: AsyncSession,
    ) -> AsyncIterator[CandidatePair]:
        """Candidate pairs for ONE chunk (the scan's inner loop; §10)."""
        if not version_ids:
            return
        query_vector = await chunk_repo.get_chunk_embedding(chunk_a.chunk_id)
        if query_vector is None:
            return  # not yet embedded — never a candidate source
        results = await chunk_repo.semantic_search(
            organization_id=organization_id,
            version_ids=version_ids,
            query_vector=query_vector,
            top_k=CANDIDATE_TOP_K,
            exclude_document_id=chunk_a.document_id,
        )
        for result in results:
            if result.similarity < CANDIDATE_SIMILARITY_THRESHOLD:
                continue
            # Pair-ordering rule: evaluate each unordered pair exactly once
            # (chunk IDs compare as strings — §10).
            if result.chunk_id <= chunk_a.chunk_id:
                continue
            yield CandidatePair(
                chunk_a=chunk_a,
                chunk_b=_statement_chunk(result),
                similarity=result.similarity,
            )

    # ── §11: contradiction verification ───────────────────────────────────────

    @staticmethod
    async def check_contradiction(
        chunk_a: StatementChunk,
        chunk_b: StatementChunk,
        *,
        provider: LLMProvider | None,
    ) -> ContradictionResult | None:
        """The bounded contradiction-check LLM call (§11).

        Returns None on any provider/parse failure — the caller skips the
        candidate and continues; it NEVER raises into the scan loop.
        """
        if provider is None:
            return None

        text_a, _ = _clip_to_token_budget(
            chunk_a.content, CONTRADICTION_CHECK_TOKEN_BUDGET
        )
        text_b, _ = _clip_to_token_budget(
            chunk_b.content, CONTRADICTION_CHECK_TOKEN_BUDGET
        )

        messages = [
            LLMMessage(role="system", content=CONTRADICTION_CHECK_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=CONTRADICTION_CHECK_USER_TEMPLATE.format(
                    statement_a=text_a, statement_b=text_b
                ),
            ),
        ]

        from app.rag.query_analyzer import _FAST_TIMEOUT_SECONDS

        try:
            response = await provider.generate(
                messages,
                temperature=0.0,
                max_tokens=200,
                stream=False,
                timeout=_FAST_TIMEOUT_SECONDS,
            )
        except LLMProviderError as exc:
            logger.warning(
                "Contradiction check LLM call failed (%s) — candidate skipped",
                exc.code,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — never raise into the scan loop
            logger.warning(
                "Contradiction check unexpected error (%s) — candidate skipped", exc
            )
            return None

        return parse_contradiction_check(response.content)

    # ── §12: live effective-date classification ───────────────────────────────

    @staticmethod
    async def load_version_states(
        statements: list[ConflictStatement], db: AsyncSession
    ) -> dict[str, Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]]:
        """Live version-state per statement (computed, NEVER persisted — §12).

        One query loads every version of every involved document; each
        statement's version is then classified against its document's full
        version list via Phase 12's ``classify_version_state``.
        """
        states: dict[str, Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]] = {}
        if not statements:
            return states

        document_ids = sorted({s.document_id for s in statements})
        versions_result = await db.execute(
            select(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
        )
        by_document: dict[str, list[DocumentVersion]] = {}
        version_by_id: dict[str, DocumentVersion] = {}
        for version in versions_result.scalars().all():
            by_document.setdefault(version.document_id, []).append(version)
            version_by_id[version.id] = version

        for statement in statements:
            version = version_by_id.get(statement.document_version_id)
            if version is None:
                # RESTRICT FKs make this structurally impossible today —
                # classify defensively as non-current (de-prioritizes).
                states[statement.id] = "SUPERSEDED"
                continue
            states[statement.id] = classify_version_state(
                version, by_document.get(version.document_id, [])
            )
        return states

    @staticmethod
    async def classify_conflict_priority(
        conflict: Conflict,
        statements: list[ConflictStatement],
        db: AsyncSession,
    ) -> Literal["ACTIVE", "LIKELY_RESOLVED"]:
        """ACTIVE only when EVERY statement's version is CURRENT (§12).

        Any non-current side de-prioritizes the whole conflict — the live,
        never-persisted mapping onto FE §6.13's "Likely resolved by version
        update" hint.
        """
        states = await ConflictService.load_version_states(statements, db)
        for statement in statements:
            if states.get(statement.id) != "CURRENT":
                return "LIKELY_RESOLVED"
        return "ACTIVE"

    # ── §14: persistence, dedup/merge ─────────────────────────────────────────

    @staticmethod
    async def persist_or_merge(
        *,
        organization_id: str,
        chunk_a: Any,
        chunk_b: Any,
        detection_result: ContradictionResult,
        detection_method: str,
        db: AsyncSession,
        critical_patterns: list[str] | None = None,
        topic: str | None = None,
        commit: bool = False,
    ) -> Conflict | None:
        """The single deterministic write path both triggers call (§14).

        Dedup identity is chunk membership:
          1. exact pair already recorded (ANY status) → no-op (never reopen);
          2. one chunk belongs to an OPEN conflict → grow it (only OPEN —
             resolved conflicts are terminal and never re-litigated);
          3. both chunks belong to two DIFFERENT open conflicts → log + skip
             (V1 does not merge conflict graphs);
          4. otherwise → create a brand-new conflict with 2 statements.

        Returns the created/grown Conflict, or None for a no-op/skip.
        """
        repo = ConflictRepository(db)
        side_a = _statement_chunk(chunk_a)
        side_b = _statement_chunk(chunk_b)

        # 1. Exact-pair dedup — any status.
        existing = await repo.find_by_exact_pair(
            organization_id, side_a.chunk_id, side_b.chunk_id
        )
        if existing is not None:
            return None

        # 2./3. Growth and collision handling.
        open_a = await repo.find_open_by_single_chunk(organization_id, side_a.chunk_id)
        open_b = await repo.find_open_by_single_chunk(organization_id, side_b.chunk_id)

        if (
            open_a is not None
            and open_b is not None
            and open_a.id != open_b.id
        ):
            logger.info(
                "Candidate pair spans two distinct open conflicts (%s / %s) — "
                "skipping (V1 does not merge).",
                open_a.id, open_b.id,
            )
            return None

        if open_a is not None and open_b is not None and open_a.id == open_b.id:
            # Both chunks already statements of the SAME open conflict —
            # nothing to add (the exact pair just was not queried together).
            return open_a

        try:
            if open_a is not None or open_b is not None:
                target = open_a if open_a is not None else open_b
                assert target is not None
                new_chunk = side_b if open_a is not None else side_a
                await ConflictService._add_statement(db, target, new_chunk, repo)
                if commit:
                    await db.commit()
                return target

            # 4. Brand-new conflict with exactly 2 statements.
            topic = topic or detection_result.conflict_topic or (
                _fallback_topic(side_a, side_b)
            )
            is_critical = await ConflictService._any_side_is_critical(
                organization_id, [side_a, side_b], db, critical_patterns
            )
            severity = classify_conflict_severity(
                is_critical_section=is_critical,
                confidence=detection_result.confidence,
                statement_count=2,
            )
            conflict = await repo.create(
                organization_id=organization_id,
                topic=topic,
                severity=severity,
                detection_method=detection_method,
            )
            for side in (side_a, side_b):
                await ConflictService._add_statement(db, conflict, side, repo)
            if commit:
                await db.commit()
            return conflict
        except IntegrityError:
            # Concurrent-insert race: the unique constraint is the backstop
            # (§14) — treat as "already added", re-fetch, never a 500.
            await db.rollback()
            existing = await repo.find_by_exact_pair(
                organization_id, side_a.chunk_id, side_b.chunk_id
            )
            if existing is not None:
                logger.info(
                    "Concurrent conflict insert resolved via unique constraint "
                    "(conflict=%s)", existing.id,
                )
                return existing
            raise

    @staticmethod
    async def _add_statement(
        db: AsyncSession,
        conflict: Conflict,
        side: StatementChunk,
        repo: ConflictRepository,
    ) -> None:
        """Persist one statement row (denormalized snapshot + display date)."""
        # effective_date: static denormalized display value from the source
        # version (never the live classification source — §8/§12).
        version_result = await db.execute(
            select(DocumentVersion.effective_date).where(
                DocumentVersion.id == side.document_version_id
            )
        )
        effective_date = version_result.scalar_one_or_none()
        await repo.add_statement(
            conflict.id,
            document_id=side.document_id,
            document_version_id=side.document_version_id,
            chunk_id=side.chunk_id,
            page_id=side.page_id,
            page_number=side.page_number,
            statement_text=side.content,
            section=_section_label(side),
            effective_date=effective_date,
        )

    @staticmethod
    async def _any_side_is_critical(
        organization_id: str,
        sides: list[StatementChunk],
        db: AsyncSession,
        critical_patterns: list[str] | None,
    ) -> bool:
        """Critical-section signal via the SHARED Phase 12 lookup (§12)."""
        if critical_patterns is None:
            critical_patterns = await ConflictService.load_critical_patterns(
                organization_id, db
            )
        if not critical_patterns:
            return False
        for side in sides:
            if is_critical_section(
                side.section_title, side.section_number, critical_patterns
            ):
                return True
        return False

    @staticmethod
    async def load_critical_patterns(
        organization_id: str, db: AsyncSession
    ) -> list[str]:
        """Org's ``settings["comparison"]["critical_sections"]`` patterns.

        The SAME key (and shape) the comparison severity rule reads — never
        a second, conflict-specific settings key (§4.2).
        """
        result = await db.execute(
            select(Organization.settings).where(Organization.id == organization_id)
        )
        settings = result.scalar_one_or_none()
        if not isinstance(settings, dict):
            return []
        try:
            patterns = (settings or {}).get("comparison", {}).get(
                "critical_sections", []
            )
        except Exception:  # noqa: BLE001 — malformed settings must not break scans
            return []
        return [p for p in patterns if isinstance(p, str) and p.strip()]

    # ── §13: comparison-derived seeding ───────────────────────────────────────

    @staticmethod
    async def seed_from_comparison(comparison_id: str, db: AsyncSession) -> int:
        """Seed conflicts from a completed comparison's qualifying changes.

        Qualification (deterministic, §13): change_type == MODIFIED AND
        severity == MAJOR (Phase 12's strictest tier as the critical/material
        proxy) AND both comparison sides are CURRENT right now.  NO LLM call
        — Phase 12's pipeline already established the material change.
        Loose coupling: re-queries by ID, receives no in-memory objects.

        Returns the number of conflicts actually seeded (0 on dedup no-ops).
        """
        from app.repositories.document_comparison_repository import (
            DocumentComparisonRepository,
        )

        comp_repo = DocumentComparisonRepository(db)
        comparison = await comp_repo.get_by_id(comparison_id)
        if comparison is None or comparison.status != "COMPLETED":
            return 0

        changes = await comp_repo.list_changes(comparison_id)
        if not changes:
            return 0

        # Both comparison sides must be CURRENT right now (§13 stricter rule)
        version_ids = [comparison.document_a_version_id, comparison.document_b_version_id]
        versions_result = await db.execute(
            select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
        )
        version_by_id = {v.id: v for v in versions_result.scalars().all()}
        versions_by_document: dict[str, list[DocumentVersion]] = {}
        for version in version_by_id.values():
            versions_by_document.setdefault(version.document_id, []).append(version)
        for version_id in version_ids:
            version = version_by_id.get(version_id)
            if version is None:
                return 0
            state = classify_version_state(
                version, versions_by_document.get(version.document_id, [])
            )
            if state != "CURRENT":
                logger.info(
                    "seed_from_comparison: comparison %s side %s is %s — no conflict "
                    "seeded (version history, not a live disagreement)",
                    comparison_id, version_id, state,
                )
                return 0

        organization_id = comparison.organization_id
        critical_patterns = await ConflictService.load_critical_patterns(
            organization_id, db
        )

        # Chunk provenance for statement rows (shared Phase 12 join).
        from app.services.comparison_service import ComparisonService

        chunk_ids = sorted(
            {
                cid
                for change in changes
                for cid in (change.old_chunk_id, change.new_chunk_id)
                if cid
            }
        )
        provenance = await ComparisonService.resolve_chunk_provenance(db, chunk_ids)

        seeded = 0
        for change in changes:
            if change.change_type != "MODIFIED":
                continue
            if change.severity != "MAJOR":
                continue
            if change.old_chunk_id is None or change.new_chunk_id is None:
                continue  # defensive; MODIFIED always has both

            old_info = provenance.get(change.old_chunk_id)
            new_info = provenance.get(change.new_chunk_id)
            if old_info is None or new_info is None:
                continue  # a side's chunk is gone — cannot evidence a statement

            topic = f"{change.section or 'Document'} — Version Discrepancy"
            detection_result = ContradictionResult(
                is_conflict=True,
                confidence=1.0,
                reason="Material change between two currently-effective versions.",
                conflict_topic=topic,
            )
            conflict = await ConflictService.persist_or_merge(
                organization_id=organization_id,
                chunk_a=_statement_chunk(
                    {"chunk_id": change.old_chunk_id, **old_info}
                ),
                chunk_b=_statement_chunk(
                    {"chunk_id": change.new_chunk_id, **new_info}
                ),
                detection_result=detection_result,
                detection_method="COMPARISON_DERIVED",
                db=db,
                critical_patterns=critical_patterns,
                topic=topic,
            )
            if conflict is not None:
                seeded += 1

        if seeded:
            await db.commit()
            logger.info(
                "seed_from_comparison: comparison=%s seeded=%d conflict(s)",
                comparison_id, seeded,
            )
        return seeded

    # ── §15: background scan ──────────────────────────────────────────────────

    @staticmethod
    async def run_scan(
        *,
        organization_id: str,
        job: Any,
        db: AsyncSession,
        provider: LLMProvider | None = None,
    ) -> dict[str, Any]:
        """The checkpointed, resumable org-wide scan loop (§15).

        Checkpoint granularity is DOCUMENT-level (deliberate simplification):
        after each fully-processed document the checkpoint + progress are
        committed, so a crash resumes from the cursor's next document.
        Dedup (persist_or_merge) makes re-processing entirely safe either way.
        """
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        if provider is None:
            from app.infrastructure.llm import get_llm_provider

            try:
                provider = get_llm_provider()
            except Exception:  # noqa: BLE001
                provider = None

        chunk_repo = DocumentChunkRepository(db)
        repo = ConflictRepository(db)

        checkpoint: dict[str, Any] = dict(
            job.checkpoint
            or {
                "cursor_document_id": None,
                "documents_total": 0,
                "documents_scanned": 0,
                "candidates_evaluated": 0,
                "conflicts_created": 0,
            }
        )
        checkpoint.setdefault("cursor_document_id", None)
        checkpoint.setdefault("documents_total", 0)
        checkpoint.setdefault("documents_scanned", 0)
        checkpoint.setdefault("candidates_evaluated", 0)
        checkpoint.setdefault("conflicts_created", 0)

        cursor = checkpoint["cursor_document_id"]
        # Resume point: documents AFTER the cursor (ordered by id — §15)
        stmt = (
            select(Document)
            .where(
                Document.organization_id == organization_id,
                Document.deleted_at.is_(None),
                Document.status == "active",
            )
            .order_by(Document.id)
        )
        if cursor:
            stmt = stmt.where(Document.id > cursor)
        docs_result = await db.execute(stmt)
        documents = list(docs_result.scalars().all())

        documents_total = checkpoint["documents_scanned"] + len(documents)
        checkpoint["documents_total"] = documents_total

        current_by_document, _ = await ConflictService.resolve_current_versions(
            organization_id, db
        )
        all_current_version_ids = [v.id for v in current_by_document.values()]

        for document in documents:
            current_version = current_by_document.get(document.id)
            if current_version is not None and all_current_version_ids:
                refs = await chunk_repo.list_embedded_chunk_refs_for_version(
                    current_version.id
                )
                for ref in refs:
                    ref["document_id"] = document.id
                    chunk_a = _statement_chunk(ref)
                    async for pair in ConflictService.generate_candidates_for_chunk(
                        chunk_a,
                        organization_id=organization_id,
                        version_ids=all_current_version_ids,
                        chunk_repo=chunk_repo,
                        db=db,
                    ):
                        checkpoint["candidates_evaluated"] += 1
                        result = await ConflictService.check_contradiction(
                            pair.chunk_a, pair.chunk_b, provider=provider
                        )
                        if (
                            result is not None
                            and result.is_conflict
                            and result.confidence >= CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD
                        ):
                            conflict = await ConflictService.persist_or_merge(
                                organization_id=organization_id,
                                chunk_a=pair.chunk_a,
                                chunk_b=pair.chunk_b,
                                detection_result=result,
                                detection_method="BACKGROUND_SCAN",
                                db=db,
                            )
                            if conflict is not None:
                                checkpoint["conflicts_created"] += 1

            # Per-document incremental commit — resumable (§15, Backend §49).
            checkpoint["cursor_document_id"] = document.id
            checkpoint["documents_scanned"] = checkpoint.get("documents_scanned", 0) + 1
            # Assign a COPY: SQLAlchemy's change detection compares by
            # identity for mutable JSONB values — re-assigning the same
            # in-place-mutated dict would never dirty the column.
            job.checkpoint = dict(checkpoint)
            job.progress = int(
                100 * checkpoint["documents_scanned"] / max(documents_total, 1)
            )
            await db.commit()

        logger.info(
            "run_scan: org=%s job=%s documents=%d candidates=%d conflicts=%d",
            organization_id,
            getattr(job, "id", "?"),
            checkpoint["documents_scanned"],
            checkpoint["candidates_evaluated"],
            checkpoint["conflicts_created"],
        )
        return checkpoint

    # ── §16/§17: resolution + audit ───────────────────────────────────────────

    @staticmethod
    async def resolve(
        conflict_id: str,
        *,
        user: User,
        decision: Literal["REVIEWED", "DISMISSED"],
        note: str | None,
        db: AsyncSession,
    ) -> Conflict:
        """The audited, role-gated terminal transition (§16).

        Authorization is layered:
          1. conflict:resolve permission (live DB re-check — Admin/Editor);
          2. org scoping + the private-source exclusion — an unreadable or
             cross-org conflict is indistinguishable from a nonexistent one
             (404, never 403 — no existence leakage).
        """
        if decision not in ("REVIEWED", "DISMISSED"):
            raise ValidationError("decision must be REVIEWED or DISMISSED.")

        await AuthorizationService.check_permission(
            user=user, permission_key=PermissionKey.CONFLICT_RESOLVE, db=db
        )

        repo = ConflictRepository(db)
        conflict = await repo.get_authorized_for_org(
            conflict_id, user.organization_id, user.id
        )
        if conflict is None:
            raise NotFoundError("Conflict not found.")

        if conflict.status != "OPEN":
            raise ConflictError("This conflict has already been resolved.")

        await repo.resolve(
            conflict,
            status=decision,
            resolved_by=user.id,
            resolution_note=note,
        )
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.CONFLICT_RESOLVED,
            resource_type="conflict",
            resource_id=conflict.id,
            metadata={"decision": decision, "note": note},
        )
        await db.commit()
        logger.info(
            "Conflict %s resolved (%s) by user %s", conflict.id, decision, user.id
        )
        return conflict

    # ── §18: scan status ──────────────────────────────────────────────────────

    @staticmethod
    async def get_scan_status(
        *, organization_id: str, db: AsyncSession
    ) -> dict[str, Any]:
        """Scan-status snapshot for GET /conflicts/scan-status (§18)."""
        from app.models.processing_job import ProcessingJob

        latest_result = await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.organization_id == organization_id,
                ProcessingJob.job_type == "CONFLICT_SCAN",
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        latest = latest_result.scalar_one_or_none()

        last_completed_result = await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.organization_id == organization_id,
                ProcessingJob.job_type == "CONFLICT_SCAN",
                ProcessingJob.status == "COMPLETED",
            )
            .order_by(ProcessingJob.completed_at.desc().nullslast())
            .limit(1)
        )
        last_completed = last_completed_result.scalar_one_or_none()

        last_scan_status: str | None = None
        last_scan_completed_at = None
        conflicts_created: int | None = None
        if latest is not None:
            last_scan_status = latest.status
        if last_completed is not None:
            last_scan_completed_at = last_completed.completed_at
            checkpoint = last_completed.checkpoint or {}
            if isinstance(checkpoint, dict):
                conflicts_created = int(checkpoint.get("conflicts_created") or 0)

        # Unscanned = active documents created after the last completed scan
        stmt = select(func.count(Document.id)).where(
            Document.organization_id == organization_id,
            Document.deleted_at.is_(None),
            Document.status == "active",
        )
        if last_completed is not None and last_completed.completed_at is not None:
            stmt = stmt.where(Document.created_at > last_completed.completed_at)
        unscanned_result = await db.execute(stmt)
        unscanned_count = int(unscanned_result.scalar_one())

        return {
            "last_scan_status": last_scan_status,
            "last_scan_completed_at": last_scan_completed_at,
            "last_scan_conflicts_created": conflicts_created,
            "unscanned_document_count": unscanned_count,
        }

    # ── §18: list/detail serialization ───────────────────────────────────────

    @staticmethod
    async def list_conflicts(
        *,
        organization_id: str,
        requesting_user: User,
        status: str | None = "OPEN",
        severity: str | None = None,
        db: AsyncSession,
    ) -> list[dict[str, Any]]:
        """Authorized conflict summaries with live priority (§12, §18)."""
        repo = ConflictRepository(db)
        conflicts = await repo.list_for_org(
            organization_id,
            status=status,
            severity=severity,
            requesting_user_id=requesting_user.id,
        )
        if not conflicts:
            return []

        counts = await ConflictService._statement_counts(
            [c.id for c in conflicts], db
        )
        statements_by_conflict = await ConflictService._statements_for_conflicts(
            [c.id for c in conflicts], db
        )
        all_statements = [s for sts in statements_by_conflict.values() for s in sts]
        states = await ConflictService.load_version_states(all_statements, db)

        summaries: list[dict[str, Any]] = []
        for conflict in conflicts:
            statements = statements_by_conflict.get(conflict.id, [])
            priority = "ACTIVE"
            for statement in statements:
                if states.get(statement.id) != "CURRENT":
                    priority = "LIKELY_RESOLVED"
                    break
            summaries.append(
                {
                    "id": conflict.id,
                    "topic": conflict.topic,
                    "severity": conflict.severity,
                    "status": conflict.status,
                    "detection_method": conflict.detection_method,
                    "priority": priority,
                    "statement_count": counts.get(conflict.id, 0),
                    "detected_at": conflict.detected_at,
                }
            )
        return summaries

    @staticmethod
    async def get_conflict_detail(
        *,
        conflict_id: str,
        requesting_user: User,
        db: AsyncSession,
    ) -> dict[str, Any] | None:
        """Authorized conflict detail embedding statements (§18 — no separate
        /statements endpoint; the roadmap's literal endpoint list)."""
        repo = ConflictRepository(db)
        conflict = await repo.get_authorized_for_org(
            conflict_id, requesting_user.organization_id, requesting_user.id
        )
        if conflict is None:
            return None

        statements = await repo.list_statements(conflict.id)
        states = await ConflictService.load_version_states(statements, db)
        priority = "ACTIVE"
        for statement in statements:
            if states.get(statement.id) != "CURRENT":
                priority = "LIKELY_RESOLVED"
                break

        document_names = await ConflictService._document_names(
            {s.document_id for s in statements}, db
        )
        version_numbers = await ConflictService._version_numbers(
            {s.document_version_id for s in statements}, db
        )

        return {
            "id": conflict.id,
            "topic": conflict.topic,
            "severity": conflict.severity,
            "status": conflict.status,
            "detection_method": conflict.detection_method,
            "priority": priority,
            "statement_count": len(statements),
            "detected_at": conflict.detected_at,
            "resolved_by": conflict.resolved_by,
            "resolution_note": conflict.resolution_note,
            "resolved_at": conflict.resolved_at,
            "statements": [
                {
                    "id": s.id,
                    "document_id": s.document_id,
                    "document_version_id": s.document_version_id,
                    "document_name": document_names.get(s.document_id, "Unknown document"),
                    "version_number": version_numbers.get(s.document_version_id, 1),
                    "chunk_id": s.chunk_id,
                    "page_number": s.page_number,
                    "section": s.section,
                    "statement_text": s.statement_text,
                    "effective_date": s.effective_date,
                    "version_state": states.get(s.id, "SUPERSEDED"),
                }
                for s in statements
            ],
        }

    @staticmethod
    async def _statement_counts(
        conflict_ids: list[str], db: AsyncSession
    ) -> dict[str, int]:
        result = await db.execute(
            select(
                ConflictStatement.conflict_id,
                func.count(ConflictStatement.id),
            )
            .where(ConflictStatement.conflict_id.in_(conflict_ids))
            .group_by(ConflictStatement.conflict_id)
        )
        return {conflict_id: int(count) for conflict_id, count in result.all()}

    @staticmethod
    async def _statements_for_conflicts(
        conflict_ids: list[str], db: AsyncSession
    ) -> dict[str, list[ConflictStatement]]:
        result = await db.execute(
            select(ConflictStatement)
            .where(ConflictStatement.conflict_id.in_(conflict_ids))
            .order_by(ConflictStatement.created_at)
        )
        grouped: dict[str, list[ConflictStatement]] = {}
        for statement in result.scalars().all():
            grouped.setdefault(statement.conflict_id, []).append(statement)
        return grouped

    @staticmethod
    async def _document_names(
        document_ids: set[str], db: AsyncSession
    ) -> dict[str, str]:
        if not document_ids:
            return {}
        result = await db.execute(
            select(Document.id, Document.name).where(Document.id.in_(document_ids))
        )
        return {doc_id: name for doc_id, name in result.all()}

    @staticmethod
    async def _version_numbers(
        version_ids: set[str], db: AsyncSession
    ) -> dict[str, int]:
        if not version_ids:
            return {}
        result = await db.execute(
            select(DocumentVersion.id, DocumentVersion.version_number).where(
                DocumentVersion.id.in_(version_ids)
            )
        )
        return {version_id: number for version_id, number in result.all()}

    # ── §19: CONFLICT_DETECTION chat support ─────────────────────────────────

    @staticmethod
    async def list_for_chat(
        *,
        organization_id: str,
        accessible_document_ids: list[str] | None,
        requesting_user: User | None = None,
        topic_hint: list[str] | None = None,
        db: AsyncSession,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Authorized OPEN conflicts for the chat narration path (§19).

        A conflict qualifies only when EVERY statement's document is in the
        conversation's already-resolved accessible set (the same authorization
        principle as the list endpoint, applied to the pre-computed scope —
        never recomputed, never broadened).  ``accessible_document_ids=None``
        means a knowledge-base scope: the repository's private-source
        exclusion filter applies instead (any org member's view).
        ``topic_hint`` (analyzer scope hints) is advisory: it narrows by
        case-insensitive topic substring, never expands.

        Payload statements carry full provenance (document/chunk/version/
        page IDs) so the conversational layer can attach citations without
        a second query.
        """
        repo = ConflictRepository(db)

        if accessible_document_ids is None:
            conflicts = await repo.list_for_org(
                organization_id,
                status="OPEN",
                requesting_user_id=requesting_user.id if requesting_user else "",
            )
        else:
            if not accessible_document_ids:
                return []
            from sqlalchemy import exists as sa_exists

            inaccessible = (
                select(ConflictStatement.id).where(
                    ConflictStatement.conflict_id == Conflict.id,
                    ConflictStatement.document_id.not_in(
                        list(accessible_document_ids)
                    ),
                )
            )
            stmt = (
                select(Conflict)
                .where(
                    repo._org_filter(organization_id),
                    Conflict.status == "OPEN",
                    ~sa_exists(inaccessible),
                )
                .order_by(Conflict.detected_at.desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            conflicts = list(result.scalars().all())

        if not conflicts:
            return []

        if topic_hint:
            hints = [h.lower() for h in topic_hint if isinstance(h, str) and h.strip()]
            if hints:
                conflicts = [
                    c for c in conflicts
                    if any(h in (c.topic or "").lower() for h in hints)
                ]
                if not conflicts:
                    return []

        statements_by_conflict = await ConflictService._statements_for_conflicts(
            [c.id for c in conflicts], db
        )
        document_names = await ConflictService._document_names(
            {s.document_id for sts in statements_by_conflict.values() for s in sts},
            db,
        )

        payload: list[dict[str, Any]] = []
        for conflict in conflicts:
            statements = statements_by_conflict.get(conflict.id, [])
            payload.append(
                {
                    "id": conflict.id,
                    "topic": conflict.topic,
                    "severity": conflict.severity,
                    "detection_method": conflict.detection_method,
                    "statements": [
                        {
                            "document_id": s.document_id,
                            "document_version_id": s.document_version_id,
                            "chunk_id": s.chunk_id,
                            "page_id": s.page_id,
                            "page_number": s.page_number,
                            "document_name": document_names.get(
                                s.document_id, "Unknown document"
                            ),
                            "section": s.section,
                            "statement_text": s.statement_text,
                            "effective_date": (
                                s.effective_date.isoformat()
                                if s.effective_date
                                else None
                            ),
                        }
                        for s in statements
                    ],
                }
            )
        return payload

    # ── §20: inline RAG surfacing ─────────────────────────────────────────────

    @staticmethod
    async def find_conflicts_among_chunks(
        *,
        organization_id: str,
        chunk_ids: list[str],
        db: AsyncSession,
    ) -> list[ConflictNotice]:
        """OPEN conflicts with >=2 chunks among the retrieved set (§20).

        Purely deterministic — populated from the DB, never from LLM output,
        so the FE's conflict notice cannot be hallucinated.  No additional
        authorization check is needed here BY DESIGN: the retrieved chunk set
        is already permission-scoped upstream by the retriever (§20).
        """
        if len(chunk_ids) < 2:
            return []

        repo = ConflictRepository(db)
        mapping = await repo.conflict_ids_for_chunks(organization_id, chunk_ids)
        counts: dict[str, int] = {}
        for conflict_ids in mapping.values():
            for conflict_id in set(conflict_ids):
                counts[conflict_id] = counts.get(conflict_id, 0) + 1
        qualifying = [
            conflict_id
            for conflict_id, count in counts.items()
            if count >= 2
        ]
        if not qualifying:
            return []

        result = await db.execute(
            select(Conflict).where(
                repo._org_filter(organization_id),
                Conflict.status == "OPEN",
                Conflict.id.in_(qualifying),
            )
        )
        return [
            ConflictNotice(
                conflict_id=conflict.id,
                topic=conflict.topic,
                severity=conflict.severity,
            )
            for conflict in result.scalars().all()
        ]


# ── Module helpers ────────────────────────────────────────────────────────────

def _fallback_topic(side_a: StatementChunk, side_b: StatementChunk) -> str:
    """Deterministic topic when the LLM provided no conflict_topic (§11)."""
    section_a = _section_label(side_a)
    section_b = _section_label(side_b)
    if section_a and section_b:
        return f"{section_a} / {section_b} disagreement"
    if section_a or section_b:
        return f"{section_a or section_b} disagreement"
    return "Cross-document disagreement"


def _clip_to_token_budget(text: str, budget: int) -> tuple[str, bool]:
    """Clip text to a token budget using the shared tokenizer (§11).

    Uses ``app.ingestion.tokenizer``'s TokenCounter (tiktoken with a
    documented approximation fallback).  4 chars/token is the counter's
    documented conservative floor, used as the first-cut size.
    """
    if not text:
        return text, False
    from app.ingestion.tokenizer import TokenCounter

    global _TOKEN_COUNTER
    if _TOKEN_COUNTER is None:
        _TOKEN_COUNTER = TokenCounter()
    counter = _TOKEN_COUNTER

    if counter.count_tokens(text) <= budget:
        return text, False

    clipped = text[: budget * 4]
    while clipped and counter.count_tokens(clipped) > budget:
        clipped = clipped[: len(clipped) // 2]
    return clipped + " [...]", True


_TOKEN_COUNTER: Any = None
