"""
ComparisonService — document version comparison orchestration (Phase 12).

Responsibilities:
  - get_or_create_comparison(): idempotent entry point — authorizes both
    version sides, normalizes the pair order, reuses existing comparisons,
    creates PENDING row + COMPARISON job atomically on first call.
  - resolve_comparison_targets_from_chat(): deterministic, non-LLM algorithm
    for resolving two version IDs from a chat message context (§14).

Worker-side pipeline (handle_comparison in workers/jobs.py) is separate.
This service is for API/chat orchestration only.

See PHASE-12-IMPLEMENTATION-PLAN.md §11 Task 6, §14.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.domain.versioning import resolve_current_version
from app.models.comparison import DocumentComparison
from app.models.user import User
from app.repositories.document_comparison_repository import DocumentComparisonRepository
from app.services.authorization_service import AuthorizationService
from app.services.job_service import JobService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ComparisonService:
    """All document-comparison business logic (static methods, explicit session)."""

    # ── Creation + reuse ───────────────────────────────────────────────────────

    @staticmethod
    async def get_or_create_comparison(
        *,
        user: User,
        document_a_version_id: str,
        document_b_version_id: str,
        db: AsyncSession,
    ) -> tuple[DocumentComparison, bool]:
        """Idempotent comparison creation.

        Returns:
            (comparison, created) — ``created=False`` when reusing an existing
            comparison row; ``created=True`` when a new PENDING row was inserted.

        Raises:
            ValidationError (422): identical version IDs, or either version
                not in READY status.
            NotFoundError (404): either version ID does not exist or is not
                authorized for the requesting user.
        """
        # 1. Reject identical IDs (422)
        if document_a_version_id == document_b_version_id:
            raise ValidationError("Cannot compare a version with itself.")

        # 2. Authorize both sides — raises NotFoundError on failure (§9.2)
        version_a, document_a = await AuthorizationService.authorize_document_version(
            user, document_a_version_id, db
        )
        version_b, document_b = await AuthorizationService.authorize_document_version(
            user, document_b_version_id, db
        )

        # 3. Both versions must be READY
        if version_a.status != "READY":
            raise ValidationError(
                f"Version {document_a_version_id} is not READY (status={version_a.status})."
            )
        if version_b.status != "READY":
            raise ValidationError(
                f"Version {document_b_version_id} is not READY (status={version_b.status})."
            )

        # 4. Normalize pair order (stable UNIQUE constraint) — plan Gap 4:
        #    the (document_id, version_number) tuple is compared
        #    lexicographically (string document_id, integer version_number),
        #    so within one document the LOWER version number is always side
        #    A ("old"), regardless of the caller's argument order.  This is
        #    deterministic for cross-document pairs too.
        key_a = (version_a.document_id, version_a.version_number)
        key_b = (version_b.document_id, version_b.version_number)
        if key_a <= key_b:
            norm_a, norm_b = document_a_version_id, document_b_version_id
        else:
            norm_a, norm_b = document_b_version_id, document_a_version_id

        repo = DocumentComparisonRepository(db)

        # 5. Reuse check
        existing = await repo.get_by_pair(user.organization_id, norm_a, norm_b)
        if existing is not None:
            return existing, False

        # 6. Create PENDING comparison row + COMPARISON job atomically
        try:
            comparison = await repo.create(
                organization_id=user.organization_id,
                version_a_id=norm_a,
                version_b_id=norm_b,
                requested_by=user.id,
            )
            job = await JobService.create_for_comparison(
                db,
                organization_id=user.organization_id,
                comparison_id=comparison.id,
                anchor_version_id=norm_a,  # version A is the "anchor"
            )
            await db.commit()
        except IntegrityError:
            # Concurrent duplicate request hit the unique constraint —
            # roll back and re-fetch the row the other request created.
            await db.rollback()
            existing = await repo.get_by_pair(user.organization_id, norm_a, norm_b)
            if existing is None:
                raise  # unexpected DB error — re-raise
            logger.info(
                "Concurrent comparison creation resolved via unique constraint; "
                "returning existing comparison %s",
                existing.id,
            )
            return existing, False

        # 7. Enqueue AFTER commit (pointer durability — Backend §50)
        await JobService.enqueue_after_commit(job)

        logger.info(
            "Comparison %s created: org=%s a=%s b=%s job=%s",
            comparison.id,
            user.organization_id,
            norm_a,
            norm_b,
            job.id,
        )
        return comparison, True

    # ── Chat intent resolution ─────────────────────────────────────────────────

    @staticmethod
    async def resolve_comparison_targets_from_chat(
        conversation: object,
        analysis: object,
        db: AsyncSession,
    ) -> tuple[str, str] | None:
        """Deterministic version-ID resolution for COMPARISON/CHANGE_DETECTION chat intents.

        Never delegates to the LLM — this is a code-driven algorithm per §14.

        Algorithm:
          1. If conversation scope has exactly 2 documents → resolve each
             document's current version (optionally using temporal_scope).
          2. Else if scope has 1 document AND analysis has two temporal
             references → resolve that document's version for each date.
          3. Otherwise → return None (caller sends clarifying message).

        Args:
            conversation: Conversation ORM instance.
            analysis:     QueryAnalysis output from analyze_query().
            db:           Active async session.

        Returns:
            (version_id_a, version_id_b) tuple, or None for clarification.
        """
        from sqlalchemy import select
        from app.models.conversation import ConversationDocument
        from app.models.document import Document, DocumentVersion

        # Fetch active conversation scope documents
        scope_type = getattr(conversation, "scope_type", None)
        conv_id = getattr(conversation, "id", None)

        stmt = select(ConversationDocument).where(
            ConversationDocument.conversation_id == conv_id,
            ConversationDocument.removed_at.is_(None),
        )
        result = await db.execute(stmt)
        conv_docs = list(result.scalars().all())

        doc_ids = [cd.document_id for cd in conv_docs]

        # ── Path 1: exactly 2 documents in scope ─────────────────────────────
        if len(doc_ids) == 2:
            versions = []
            temporal_scope = getattr(analysis, "temporal_scope", None)
            as_of = None
            if isinstance(temporal_scope, dict) and "year" in temporal_scope:
                try:
                    as_of = date(int(temporal_scope["year"]), 12, 31)
                except (ValueError, TypeError):
                    pass

            for doc_id in doc_ids:
                ver_result = await db.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == doc_id,
                        DocumentVersion.status == "READY",
                    )
                )
                doc_versions = list(ver_result.scalars().all())
                current = resolve_current_version(doc_versions, as_of=as_of)
                if current is None:
                    return None  # no valid version for one side
                versions.append(current.id)

            return tuple(versions)  # type: ignore[return-value]

        # ── Path 2: 1 document with two temporal references ───────────────────
        if len(doc_ids) == 1:
            temporal_scope = getattr(analysis, "temporal_scope", None)
            temporal_scope_secondary = getattr(analysis, "temporal_scope_secondary", None)

            if temporal_scope and temporal_scope_secondary:
                doc_id = doc_ids[0]
                ver_result = await db.execute(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == doc_id,
                        DocumentVersion.status == "READY",
                    )
                )
                doc_versions = list(ver_result.scalars().all())

                def _parse_scope(scope: dict | None) -> date | None:
                    if not isinstance(scope, dict):
                        return None
                    if "year" in scope:
                        try:
                            return date(int(scope["year"]), 12, 31)
                        except (ValueError, TypeError):
                            return None
                    return None

                as_of_a = _parse_scope(temporal_scope)
                as_of_b = _parse_scope(temporal_scope_secondary)

                if as_of_a is None or as_of_b is None:
                    return None

                ver_a = resolve_current_version(doc_versions, as_of=as_of_a)
                ver_b = resolve_current_version(doc_versions, as_of=as_of_b)

                if ver_a is None or ver_b is None:
                    return None
                if ver_a.id == ver_b.id:
                    return None  # same version — not a valid comparison

                return ver_a.id, ver_b.id

        # ── Path 3: cannot determine — caller sends clarifying message ────────
        return None

    # ── Chunk provenance resolution (§9.9) ─────────────────────────────────────

    @staticmethod
    async def resolve_chunk_provenance(
        db: AsyncSession,
        chunk_ids: list[str],
    ) -> dict[str, dict]:
        """Resolve chunk IDs into display/navigation provenance (plan §9.9).

        One joined query — chunk → page → section → version → document, the
        same join shape the citation pipeline uses for chat citations, here
        applied to arbitrary chunk IDs (comparison "View Sources" + chat
        narration citations).  Chunks deleted since the comparison ran
        (comparison FKs are SET NULL, so they would not even appear here)
        simply have no entry.

        Returns:
            {chunk_id: {document_id, document_version_id, document_name,
             version_number, page_id, page_number, section, content}}
        """
        if not chunk_ids:
            return {}

        from app.models.document import (
            Document,
            DocumentChunk,
            DocumentPage,
            DocumentSection,
            DocumentVersion,
        )
        from sqlalchemy import select

        stmt = (
            select(DocumentChunk, DocumentPage, DocumentSection, DocumentVersion, Document)
            .join(DocumentPage, DocumentChunk.page_id == DocumentPage.id)
            .outerjoin(DocumentSection, DocumentChunk.section_id == DocumentSection.id)
            .join(DocumentVersion, DocumentChunk.document_version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(DocumentChunk.id.in_(chunk_ids))
        )
        result = await db.execute(stmt)
        provenance: dict[str, dict] = {}
        for chunk, page, section, version, document in result.all():
            sec_label = None
            if section is not None:
                number = getattr(section, "section_number", None)
                title = getattr(section, "title", None)
                sec_label = (
                    f"{number} {title}" if number and title else (number or title)
                )
            provenance[chunk.id] = {
                "document_id": document.id,
                "document_version_id": version.id,
                "document_name": document.name,
                "version_number": version.version_number,
                "page_id": page.id,
                "page_number": page.page_number,
                "section": sec_label,
                "content": getattr(chunk, "content", "") or "",
            }
        return provenance
