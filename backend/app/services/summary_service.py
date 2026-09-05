"""
SummaryService — cited, structured document summary orchestration (Phase 14).

Static methods, explicit session — the exact ComparisonService/ConflictService
convention.

Responsibilities (PHASE-14-IMPLEMENTATION-PLAN.md §5.7):
  - get_or_create_summary():        idempotent entry point — authorize,
                                    require READY, reuse the version's
                                    existing row, or create a PENDING row +
                                    SUMMARY job atomically.
  - regenerate_summary():           always a fresh job; the existing row
                                    transitions back to PENDING; the old
                                    JSONB stays visible until re-completed.
  - resolve_summary_version():      API ``?version=`` resolution (None →
                                    document current version).
  - resolve_summary_target_from_chat(): deterministic single-document
                                    resolution for the SUMMARY intent.
  - run():                          the generation pipeline, called only
                                    from the worker's _run_summary_job —
                                    sampling → context → draft →
                                    resolve_citations → validate_answer →
                                    bounded regeneration → per-item
                                    claim mapping → persisted JSONB.

THE LLM NEVER WRITES TO THE DATABASE AND NEVER DECIDES WHETHER A CLAIM IS
GROUNDED: every persistence write happens only after the existing Phase 10
machinery (resolve_citations / validate_answer) has run over the flattened
draft text.

Persisted ``summary`` JSONB shape (each citable field is an explicit list —
an empty field is an explicit empty list, never an omitted key):
    {"executive_summary": [{"text": str, "citations": [citation_dict]}],
     "key_points": [...], "dates": [...], "roles": [...],
     "requirements": [...], "risks": [...],
     "topics": [str, ...]}

See PHASE-14-IMPLEMENTATION-PLAN.md §5.7, §7.1, §7.2; Backend §43.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.domain.summary_rules import (
    ChunkRef,
    SectionRef,
    select_representative_chunks,
)
from app.domain.versioning import resolve_current_version
from app.infrastructure.llm import get_llm_provider
from app.models.document import (
    Document,
    DocumentChunk,
    DocumentPage,
    DocumentSection,
    DocumentVersion,
)
from app.models.summary import DocumentSummary
from app.models.user import User
from app.rag.citations import ResolvedCitation, resolve_citations
from app.rag.citation_validator import validate_answer
from app.rag.context_builder import ContextBundle, build_context
from app.rag.retriever import SearchResult
from app.rag.summary_builder import SummaryDraft, generate_summary_draft
from app.repositories.document_section_repository import DocumentSectionRepository
from app.repositories.document_summary_repository import DocumentSummaryRepository
from app.services.authorization_service import AuthorizationService
from app.services.audit_logger import AuditAction, AuditLogger
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

# Positional epsilon: adapted chunks carry descending relevance so
# build_context's dedup/ordering stays meaningful while preserving reading
# order within a 1.0 → 0.5 band (plan §5.7 step 3).
_POSITION_EPSILON = 0.00005

# Flattened pseudo-answer fields, in render order (topics excluded — no
# citations, plan §5.7 step 6).
_CITABLE_FIELDS: tuple[str, ...] = (
    "executive_summary",
    "key_points",
    "dates",
    "roles",
    "requirements",
    "risks",
)

_CITATION_MARKER_RE = re.compile(r"\[(?:SOURCE\s*)?\d{1,2}\]")


@dataclass
class _FlatItem:
    """One citable draft item's position in the flattened pseudo-answer."""

    field: str
    item_index: int
    text: str


class SummaryService:
    """All document-summary business logic (static methods, explicit session)."""

    # ── Creation + reuse ───────────────────────────────────────────────────────

    @staticmethod
    async def get_or_create_summary(
        *,
        user: User,
        document_version_id: str,
        db: AsyncSession,
    ) -> tuple[DocumentSummary, bool]:
        """Idempotent summary creation (mirrors get_or_create_comparison).

        Returns:
            (summary, created) — ``created=False`` when reusing the version's
            existing row (regardless of status — the caller polls).

        Raises:
            ValidationError (422): the version is not READY.
            NotFoundError (404): the version does not exist or is not
                authorized (never 403 — no existence leakage).
        """
        version, document = await AuthorizationService.authorize_document_version(
            user, document_version_id, db
        )
        if version.status != "READY":
            raise ValidationError(
                f"Version {document_version_id} is not READY (status={version.status})."
            )

        repo = DocumentSummaryRepository(db)
        existing = await repo.get_by_version(document_version_id)
        if existing is not None:
            return existing, False

        try:
            summary = await repo.create(
                organization_id=user.organization_id,
                document_id=document.id,
                document_version_id=document_version_id,
                requested_by=user.id,
            )
            job = await JobService.create_for_summary(
                db,
                organization_id=user.organization_id,
                document_version_id=document_version_id,
                summary_id=summary.id,
            )
            await db.commit()
        except IntegrityError:
            # Concurrent duplicate request hit the UNIQUE(document_version_id)
            # constraint — roll back and re-fetch the row the other request
            # created (mirrors ComparisonService exactly).
            await db.rollback()
            existing = await repo.get_by_version(document_version_id)
            if existing is None:
                raise
            logger.info(
                "Concurrent summary creation resolved via unique constraint; "
                "returning existing summary %s",
                existing.id,
            )
            return existing, False

        await JobService.enqueue_after_commit(job)
        logger.info(
            "Summary %s created: org=%s version=%s job=%s",
            summary.id, user.organization_id, document_version_id, job.id,
        )
        return summary, True

    @staticmethod
    async def regenerate_summary(
        *,
        user: User,
        document_version_id: str,
        db: AsyncSession,
    ) -> DocumentSummary:
        """Always-fresh-job regeneration (permission-gated at the API layer).

        The row is created first when absent (regenerate-when-absent behaves
        like create); otherwise it transitions back to PENDING.  The existing
        ``summary`` JSONB is NOT cleared — it stays visible (dimmed, FE
        §6.12) until the new job actually completes and overwrites it.
        """
        version, document = await AuthorizationService.authorize_document_version(
            user, document_version_id, db
        )
        if version.status != "READY":
            raise ValidationError(
                f"Version {document_version_id} is not READY (status={version.status})."
            )

        repo = DocumentSummaryRepository(db)
        summary = await repo.get_by_version(document_version_id)
        if summary is None:
            summary = await repo.create(
                organization_id=user.organization_id,
                document_id=document.id,
                document_version_id=document_version_id,
                requested_by=user.id,
            )
        else:
            summary.status = "PENDING"
            summary.error_message = None
            summary.requested_by = user.id
            summary.stale = False
            await db.flush()

        job = await JobService.create_for_summary(
            db,
            organization_id=user.organization_id,
            document_version_id=document_version_id,
            summary_id=summary.id,
        )
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.SUMMARY_REGENERATED,
            resource_type="document_summary",
            resource_id=summary.id,
            metadata={"version_id": document_version_id, "job_id": job.id},
        )
        await db.commit()
        await JobService.enqueue_after_commit(job)
        logger.info(
            "Summary %s regeneration enqueued (job=%s) by user %s",
            summary.id, job.id, user.id,
        )
        return summary

    # ── Target resolution ─────────────────────────────────────────────────────

    @staticmethod
    async def resolve_summary_version(
        user: User,
        document_id: str,
        version_number: int | None,
        db: AsyncSession,
    ) -> str:
        """Resolve the API's optional ``?version=`` query param to a version id.

        ``None`` → ``document.current_version_id`` (ValidationError when the
        document has no current version yet); an explicit number → that
        specific document_versions row (404 when absent).  Always followed by
        ``authorize_document_version`` (404-not-403 on every failure).
        """
        from app.repositories.document_repository import DocumentRepository

        document = await DocumentRepository(db).get_by_id_for_org(
            document_id, user.organization_id
        )
        if document is None or getattr(document, "deleted_at", None) is not None:
            raise NotFoundError("Document not found.")

        if version_number is None:
            if document.current_version_id is None:
                raise ValidationError(
                    "This document has no current version yet."
                )
            version_id = document.current_version_id
        else:
            result = await db.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.version_number == version_number,
                )
            )
            version = result.scalar_one_or_none()
            if version is None:
                raise NotFoundError("Document version not found.")
            version_id = version.id

        await AuthorizationService.authorize_document_version(user, version_id, db)
        return version_id

    @staticmethod
    async def resolve_summary_target_from_chat(
        conversation: object,
        analysis: object,
        db: AsyncSession,
    ) -> str | None:
        """Deterministic version resolution for the SUMMARY chat intent.

        Resolves to exactly ONE document_version_id when the conversation
        scope is a single document (``analysis.temporal_scope`` refines the
        version when present, via ``resolve_current_version``).  Returns
        None in every other case (zero, or more than one, document in scope)
        — the caller sends a clarifying message, never guesses (plan §5.7).
        """
        from app.models.conversation import ConversationDocument

        scope_type = getattr(conversation, "scope_type", None)
        conv_id = getattr(conversation, "id", None)

        result = await db.execute(
            select(ConversationDocument).where(
                ConversationDocument.conversation_id == conv_id,
                ConversationDocument.removed_at.is_(None),
            )
        )
        conv_docs = list(result.scalars().all())
        if len(conv_docs) != 1:
            return None
        if scope_type not in ("current_document", "selected_documents"):
            return None

        doc_id = conv_docs[0].document_id
        ver_result = await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == doc_id,
                DocumentVersion.status == "READY",
            )
        )
        doc_versions = list(ver_result.scalars().all())
        if not doc_versions:
            return None

        as_of: date | None = None
        temporal_scope = getattr(analysis, "temporal_scope", None)
        if isinstance(temporal_scope, dict) and "year" in temporal_scope:
            try:
                as_of = date(int(temporal_scope["year"]), 12, 31)
            except (ValueError, TypeError):
                pass

        current = resolve_current_version(doc_versions, as_of=as_of)
        if current is None:
            return None
        return current.id

    # ── The generation pipeline (worker-only) ─────────────────────────────────

    @staticmethod
    async def run(job: Any, summary: DocumentSummary, session: AsyncSession) -> None:
        """The summary pipeline.  Called ONLY from ``_run_summary_job``.

        Raises on failure — the worker's retry/backoff/dead-letter machinery
        applies unchanged; the summary row is marked FAILED here first so
        the UI sees the failure even mid-retry (plan §5.7 step 11).
        """
        from app.rag.prompts import SUMMARY_PROMPT_VERSION

        repo = DocumentSummaryRepository(session)
        settings = get_settings()

        try:
            await repo.update_status(summary, "PROCESSING")
            await session.commit()

            # 1. Load sections + chunks-with-provenance for the version.
            sections = await DocumentSectionRepository(
                session
            ).list_for_version(summary.document_version_id)
            provenance_rows = await SummaryService._load_chunks_with_provenance(
                session, summary.document_version_id
            )
            if not provenance_rows:
                raise ValidationError(
                    "The document version has no processed content to summarize."
                )

            # 2. Section-diverse sampling (§5.6).
            section_refs = [
                SectionRef(
                    id=s.id,
                    parent_section_id=s.parent_section_id,
                    sort_order=s.sort_order,
                    start_page=s.start_page,
                    end_page=s.end_page,
                )
                for s in sections
            ]
            chunk_refs = [
                ChunkRef(
                    id=chunk.id,
                    section_id=chunk.section_id,
                    chunk_index=chunk.chunk_index,
                    token_count=chunk.token_count,
                    page_number=page.page_number,
                )
                for chunk, page, _section in provenance_rows
            ]
            selected_ids, disclosure = select_representative_chunks(
                section_refs,
                chunk_refs,
                summary_context_token_budget=settings.summary_context_token_budget,
                summary_sampling_chunks_per_section=(
                    settings.summary_sampling_chunks_per_section
                ),
            )

            # 3./4. Adapt to SearchResult shape + build the SOURCE context
            # (build_context reused verbatim).
            document_name, version_label = await SummaryService._load_display_labels(
                session, summary
            )
            by_id = {
                chunk.id: (chunk, page, section)
                for chunk, page, section in provenance_rows
            }
            adapted: list[SearchResult] = []
            for position, chunk_id in enumerate(selected_ids):
                entry = by_id.get(chunk_id)
                if entry is None:
                    continue
                chunk, page, section = entry
                adapted.append(
                    SummaryService._chunk_to_search_result(
                        chunk, page, section, summary.document_id,
                        document_name, position,
                    )
                )
            bundle = build_context(
                adapted, budget_tokens=settings.summary_context_token_budget
            )

            provider = get_llm_provider()
            if provider is None:
                raise RuntimeError(
                    "No LLM provider configured — summary generation unavailable."
                )

            # 5. Draft (one constrained-JSON call + one parse retry).
            draft, model, usage = await generate_summary_draft(
                bundle, document_name, version_label, provider
            )

            # 6.–9. Flatten → resolve → validate → [bounded regeneration] → map.
            summary_payload = await SummaryService._draft_to_validated_payload(
                bundle,
                draft,
                regenerate_once=lambda: generate_summary_draft(
                    bundle,
                    document_name,
                    version_label,
                    provider,
                    extra_instruction=_citation_emphasis_instruction(),
                ),
            )

            # 10. Persist COMPLETED with the surviving items + disclosure.
            await repo.update_status(
                summary,
                "COMPLETED",
                summary_json=summary_payload,
                sampling=disclosure.to_json(),
                model=model,
                prompt_version=SUMMARY_PROMPT_VERSION,
                prompt_tokens=usage.get("prompt_tokens") if usage else None,
                completion_tokens=usage.get("completion_tokens") if usage else None,
            )
            await session.commit()
            logger.info(
                "Summary %s COMPLETED (job=%s): sampled=%s strategy=%s sources=%d",
                summary.id, getattr(job, "id", "?"),
                disclosure.sampled, disclosure.strategy, len(bundle.blocks),
            )
        except Exception:
            # FAILED on the domain row FIRST (visible mid-retry), then re-raise
            # so the worker's retry/backoff policy applies unchanged (§5.7 step 11).
            await SummaryService._mark_failed_safe(repo, summary, session)
            raise

    # ── Flatten/validate machinery ────────────────────────────────────────────

    @staticmethod
    async def _draft_to_validated_payload(
        bundle: ContextBundle,
        draft: SummaryDraft,
        *,
        regenerate_once: Callable[[], Awaitable[tuple[SummaryDraft, Any, Any]]],
    ) -> dict[str, Any]:
        """Flatten → resolve_citations → validate_answer → map back to fields.

        Steps 6–9 of plan §5.7:
          - Citable fields are flattened into one ordered pseudo-answer.
          - ``resolve_citations`` + ``validate_answer`` (Phase 10 machinery,
            reused verbatim) decide survival.
          - ``should_regenerate`` → ONE bounded regeneration via
            ``regenerate_once`` (citation emphasis), re-validated with
            ``allow_regenerate=False``.
          - Items whose claim ends up uncited/unsupported are dropped —
            never fabricated, never silently kept.  A fully-stripped field
            persists as an explicit empty list (never an omitted key).
        """
        flat_items, flattened = SummaryService._flatten(draft)
        extraction = resolve_citations(flattened, bundle)
        validation = await validate_answer(extraction)

        if validation.should_regenerate:
            logger.info(
                "SummaryService: central citation failure (%s) — one bounded "
                "regeneration",
                validation.regeneration_reason,
            )
            draft, _model, _usage = await regenerate_once()
            flat_items, flattened = SummaryService._flatten(draft)
            extraction = resolve_citations(flattened, bundle)
            validation = await validate_answer(extraction, allow_regenerate=False)

        return SummaryService._build_payload(draft, flat_items, extraction, validation)

    @staticmethod
    def _flatten(draft: SummaryDraft) -> tuple[list[_FlatItem], str]:
        """Flatten citable fields into one pseudo-answer text (§5.7 step 6).

        ``executive_summary`` contributes its single item first; each list
        item becomes one sentence ending with its [N] markers (topics
        excluded).  Because every item ends with a marker, the existing
        sentence splitter treats each item as exactly one claim.
        """
        flat_items: list[_FlatItem] = []
        parts: list[str] = []

        for field_name in _CITABLE_FIELDS:
            value = getattr(draft, field_name)
            texts = (
                [value] if field_name == "executive_summary" and value.strip()
                else ([] if field_name == "executive_summary" else list(value))
            )
            for index, text in enumerate(texts):
                if not text.strip():
                    continue
                flat_items.append(
                    _FlatItem(field=field_name, item_index=index, text=text)
                )
                parts.append(text)

        return flat_items, "\n".join(parts)

    @staticmethod
    def _build_payload(
        draft: SummaryDraft,
        flat_items: list[_FlatItem],
        extraction: Any,
        validation: Any,
    ) -> dict[str, Any]:
        """Persisted JSONB after validation (§5.7 step 9).

        Claim matching is BY SENTENCE TEXT (each flattened item IS one
        sentence — the same split_sentences segmentation both the citation
        resolver and validator use), which stays correct regardless of the
        coordinate shifts caused by invalid-marker stripping.
        """
        # claim sentence (normalized, marker-stripped) → status
        claim_status: dict[str, str] = {}
        for claim in validation.claims:
            claim_status[_norm(claim.text)] = claim.status
        claim_norms = list(claim_status.keys())

        # citation's supported sentence (normalized) → citations
        citations_by_claim: dict[str, list[dict[str, Any]]] = {}
        for citation in extraction.citations:
            citations_by_claim.setdefault(_norm(citation.claim_text), []).append(
                _citation_to_dict(citation)
            )

        surviving_by_field: dict[str, list[dict[str, Any]]] = {
            field_name: [] for field_name in _CITABLE_FIELDS
        }
        for item in flat_items:
            key = _norm(item.text)
            status = claim_status.get(key)
            if status is None:
                # Degenerate text (the splitter merged/split differently):
                # associate via containment — the item survives only when
                # every claim containing it survived.
                containing = [c for c in claim_norms if key and key in c]
                if not containing:
                    continue
                if any(claim_status[c] in ("uncited", "unsupported") for c in containing):
                    continue
                item_citations = [
                    citation
                    for c in containing
                    for citation in citations_by_claim.get(c, [])
                ]
                if not item_citations:
                    continue
            else:
                if status in ("uncited", "unsupported"):
                    continue  # dropped — never fabricated, never silently kept
                item_citations = citations_by_claim.get(key, [])
                if not item_citations:
                    continue  # a persisted item always carries real provenance
            surviving_by_field[item.field].append(
                {
                    "text": _strip_markers(item.text),
                    "citations": item_citations,
                }
            )

        payload: dict[str, Any] = {
            field_name: surviving_by_field[field_name]
            for field_name in _CITABLE_FIELDS
        }
        payload["topics"] = list(draft.topics)
        return payload

    # ── Provenance helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _load_chunks_with_provenance(
        session: AsyncSession, document_version_id: str
    ) -> list[tuple[DocumentChunk, DocumentPage, DocumentSection | None]]:
        """Chunks + page + section in one join (reading order).

        The DocumentChunk.page/section relationships are lazy="noload", so
        this join is what makes page_number/section title available to the
        adapter (same join shape as ComparisonService.resolve_chunk_provenance).
        """
        stmt = (
            select(DocumentChunk, DocumentPage, DocumentSection)
            .join(DocumentPage, DocumentChunk.page_id == DocumentPage.id)
            .outerjoin(DocumentSection, DocumentChunk.section_id == DocumentSection.id)
            .where(DocumentChunk.document_version_id == document_version_id)
            .order_by(DocumentChunk.chunk_index)
        )
        result = await session.execute(stmt)
        return [(chunk, page, section) for chunk, page, section in result.all()]

    @staticmethod
    async def _load_display_labels(
        session: AsyncSession, summary: DocumentSummary
    ) -> tuple[str, str]:
        """(document_name, version_label) for the summary user message."""
        result = await session.execute(
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.id == summary.document_version_id)
        )
        row = result.first()
        if row is None:
            return "Document", "current version"
        document, version = row
        label = version.version_label or f"version {version.version_number}"
        return document.name, label

    @staticmethod
    def _chunk_to_search_result(
        chunk: DocumentChunk,
        page: DocumentPage,
        section: DocumentSection | None,
        document_id: str,
        document_name: str,
        position: int,
    ) -> SearchResult:
        """Adapt an ORM chunk row into the SearchResult shape build_context consumes.

        Positional relevance preserves reading order inside a gently
        descending band, so dedup keeps earlier (reading-order) chunks on
        ties (plan §5.7 step 3).
        """
        section_title = None
        if section is not None:
            number = getattr(section, "section_number", None)
            title = getattr(section, "title", None)
            section_title = (
                f"{number} {title}" if number and title else (number or title)
            )
        return SearchResult(
            chunk_id=chunk.id,
            document_id=document_id,
            document_version_id=chunk.document_version_id,
            document_name=document_name,
            page_id=chunk.page_id,
            page_number=page.page_number,
            section_title=section_title,
            chunk_index=chunk.chunk_index,
            snippet=(chunk.content or "")[:500],
            content=chunk.content or "",
            token_count=chunk.token_count,
            relevance=max(0.5, 1.0 - (position * _POSITION_EPSILON)),
            embedding_model=chunk.embedding_model,
            metadata=chunk.chunk_metadata or {},
        )

    @staticmethod
    async def _mark_failed_safe(
        repo: DocumentSummaryRepository,
        summary: DocumentSummary,
        session: AsyncSession,
    ) -> None:
        """Mark FAILED best-effort — never masks the original exception."""
        import sqlalchemy.exc

        try:
            await repo.update_status(
                summary,
                "FAILED",
                error_message="Summary generation failed — see job record.",
            )
            await session.commit()
        except sqlalchemy.exc.SQLAlchemyError:
            logger.exception(
                "SummaryService: FAILED transition also failed for summary %s",
                summary.id,
            )
            await session.rollback()


def _citation_emphasis_instruction() -> str:
    """The Phase 10 citation-emphasis instruction, adapted to the JSON draft."""
    from app.rag.prompts import CITATION_EMPHASIS_INSTRUCTION

    return (
        CITATION_EMPHASIS_INSTRUCTION
        + "\nRespond again with ONLY the required JSON object — every item in "
        "key_points, dates, roles, requirements, and risks must end with a [N] "
        "citation marker."
    )


def _strip_markers(text: str) -> str:
    """Clean display text (citation markers removed)."""
    return _CITATION_MARKER_RE.sub("", text).strip()


def _norm(text: str) -> str:
    """Normalization for claim/citation sentence matching."""
    return " ".join(_strip_markers(text).split()).lower()


def _citation_to_dict(citation: ResolvedCitation) -> dict[str, Any]:
    """Inline persisted citation object (everything the FE + chat re-hydration need)."""
    return {
        "index": citation.index,
        "chunk_id": citation.chunk_id,
        "document_id": citation.document_id,
        "document_version_id": citation.document_version_id,
        "document_name": citation.document_name,
        "page_id": citation.page_id,
        "page_number": citation.page_number,
        "section": citation.section,
        "relevance": citation.relevance,
        "quoted_text": citation.quoted.text,
        "char_start": citation.quoted.char_start,
        "char_end": citation.quoted.char_end,
    }
