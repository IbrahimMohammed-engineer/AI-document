"""
ExtractionService — structured-information extraction orchestration (Phase 14).

Static methods, explicit session — the ComparisonService/ConflictService
convention.

Responsibilities (PHASE-14-IMPLEMENTATION-PLAN.md §5.9):
  - create_run():                   the explicit UI/API path — ALWAYS a new
                                    PENDING run row (audit trail, never
                                    reuse; plan §2.6 point 3).
  - get_or_create_run_from_chat():  the chat-only path — reuses the latest
                                    COMPLETED run for the version when one
                                    exists (cost control for casual chat
                                    questions; plan §2.6 point 5).
  - resolve_extraction_target_from_chat(): identical single-document
                                    resolution to the summary intent.
  - get_run()/list_runs_for_document(): document:read-gated, org-scoped,
                                    404-not-403 reads.
  - run():                          the pipeline, called only from the
                                    worker's _run_extraction_job — four
                                    category retrievals → union/dedupe →
                                    context → draft → Phase 10 validation
                                    machinery → bulk-inserted items.

Extraction scope is single-document, single-version, V1; the schema is the
fixed ``standard_v1`` set (plan §2.6 points 1–2).

See PHASE-14-IMPLEMENTATION-PLAN.md §5.9, §7.3.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.domain.extraction_rules import CATEGORIES, CATEGORY_QUERIES
from app.domain.versioning import resolve_current_version
from app.infrastructure.embeddings import get_embedding_provider
from app.infrastructure.llm import get_llm_provider
from app.models.document import DocumentVersion
from app.models.extraction import DocumentExtraction
from app.models.user import User
from app.rag.citations import ResolvedCitation, resolve_citations
from app.rag.citation_validator import validate_answer
from app.rag.context_builder import ContextBundle, build_context
from app.rag.extraction_builder import (
    ExtractionDraft,
    generate_extraction_draft,
)
from app.rag.retriever import SearchResult
from app.repositories.document_chunk_repository import (
    ChunkSearchResult,
    DocumentChunkRepository,
)
from app.repositories.document_extraction_repository import (
    DocumentExtractionRepository,
)
from app.services.authorization_service import AuthorizationService
from app.services.audit_logger import AuditAction, AuditLogger
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

_CITATION_MARKER_RE = re.compile(r"\[(?:SOURCE\s*)?\d{1,2}\]")


@dataclass
class _FlatItem:
    """One citable draft item's position in the flattened pseudo-answer."""

    category: str
    item_index: int
    text: str


class ExtractionService:
    """All structured-extraction business logic (static methods, explicit session)."""

    # ── Creation (explicit run — always new) ──────────────────────────────────

    @staticmethod
    async def create_run(
        *,
        user: User,
        document_version_id: str,
        schema_key: str,
        db: AsyncSession,
    ) -> DocumentExtraction:
        """Create a new extraction run (permission-gated at the API layer).

        ALWAYS a new PENDING row + job — every explicit call is a new
        audit-trail entry (no reuse; plan §2.6 point 3).

        Raises:
            ValidationError (422): unknown schema_key, or version not READY.
            NotFoundError (404): the version does not exist or is not
                authorized (never 403 — no existence leakage).
        """
        from app.domain.extraction_rules import VALID_SCHEMA_KEYS

        if schema_key not in VALID_SCHEMA_KEYS:
            raise ValidationError(
                f"Unknown schema_key {schema_key!r} — valid: {list(VALID_SCHEMA_KEYS)}."
            )

        version, document = await AuthorizationService.authorize_document_version(
            user, document_version_id, db
        )
        if version.status != "READY":
            raise ValidationError(
                f"Version {document_version_id} is not READY (status={version.status})."
            )

        repo = DocumentExtractionRepository(db)
        extraction = await repo.create(
            organization_id=user.organization_id,
            document_id=document.id,
            document_version_id=document_version_id,
            schema_key=schema_key,
            requested_by=user.id,
        )
        job = await JobService.create_for_extraction(
            db,
            organization_id=user.organization_id,
            document_version_id=document_version_id,
            extraction_id=extraction.id,
        )
        await AuditLogger.log(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            action=AuditAction.EXTRACTION_RUN_CREATED,
            resource_type="document_extraction",
            resource_id=extraction.id,
            metadata={
                "version_id": document_version_id,
                "schema_key": schema_key,
                "job_id": job.id,
            },
        )
        await db.commit()
        await JobService.enqueue_after_commit(job)
        logger.info(
            "Extraction run %s created: org=%s version=%s job=%s",
            extraction.id, user.organization_id, document_version_id, job.id,
        )
        return extraction

    @staticmethod
    async def get_or_create_run_from_chat(
        *,
        user: User,
        document_version_id: str,
        db: AsyncSession,
    ) -> tuple[DocumentExtraction, bool]:
        """Chat-only path — reuse the latest COMPLETED run when one exists.

        This asymmetry (plan §2.6 point 5) exists specifically so a casual
        "what are the requirements in this doc" chat question never triggers
        a fresh, costly extraction run when a good one already exists.
        """
        version, _document = await AuthorizationService.authorize_document_version(
            user, document_version_id, db
        )
        if version.status != "READY":
            raise ValidationError(
                f"Version {document_version_id} is not READY (status={version.status})."
            )

        repo = DocumentExtractionRepository(db)
        latest = await repo.get_latest_completed_for_version(document_version_id)
        if latest is not None:
            return latest, False
        extraction = await ExtractionService.create_run(
            user=user,
            document_version_id=document_version_id,
            schema_key="standard_v1",
            db=db,
        )
        return extraction, True

    # ── Reads ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def get_run(
        *,
        user: User,
        extraction_id: str,
        db: AsyncSession,
    ) -> DocumentExtraction:
        """Org-scoped fetch + per-read version re-authorization (§5.15)."""
        repo = DocumentExtractionRepository(db)
        extraction = await repo.get_by_id_for_org(extraction_id, user.organization_id)
        if extraction is None:
            raise NotFoundError("Extraction run not found.")
        await AuthorizationService.authorize_document_version(
            user, extraction.document_version_id, db
        )
        return extraction

    @staticmethod
    async def list_runs_for_document(
        *,
        user: User,
        document_id: str,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[DocumentExtraction], int]:
        """Authorized run history for a document, most recent first (§5.13)."""
        from app.repositories.document_repository import DocumentRepository

        document = await DocumentRepository(db).get_by_id_for_org(
            document_id, user.organization_id
        )
        if document is None or getattr(document, "deleted_at", None) is not None:
            raise NotFoundError("Document not found.")

        result = await db.execute(
            select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
        )
        version_ids = [row[0] for row in result.all()]
        if not version_ids:
            return [], 0

        repo = DocumentExtractionRepository(db)
        runs: list[DocumentExtraction] = []
        total = 0
        for version_id in version_ids:
            await AuthorizationService.authorize_document_version(user, version_id, db)
            total += await repo.count_for_version(version_id)
            runs.extend(await repo.list_for_version(version_id))
        runs.sort(key=lambda r: r.created_at, reverse=True)
        page = runs[offset : offset + limit]
        return page, total

    # ── Target resolution ─────────────────────────────────────────────────────

    @staticmethod
    async def resolve_run_version(
        user: User,
        document_id: str,
        version_number: int | None,
        db: AsyncSession,
    ) -> str:
        """Resolve the API's optional ``version`` field to a version id.

        Mirrors SummaryService.resolve_summary_version exactly: ``None`` →
        ``document.current_version_id`` (ValidationError when absent); an
        explicit number → that specific row (404 when absent); always
        followed by ``authorize_document_version`` (404-not-403).
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
    async def resolve_extraction_target_from_chat(
        conversation: object,
        analysis: object,
        db: AsyncSession,
    ) -> str | None:
        """Identical shape/constraints to resolve_summary_target_from_chat."""
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
    async def run(job: Any, extraction: DocumentExtraction, session: AsyncSession) -> None:
        """The extraction pipeline.  Called ONLY from ``_run_extraction_job``."""
        from app.rag.prompts import EXTRACTION_PROMPT_VERSION

        repo = DocumentExtractionRepository(session)
        settings = get_settings()

        try:
            await repo.update_status(extraction, "PROCESSING")
            await session.commit()

            # 1. Four category retrievals (cheap semantic_search only — no
            #    hybrid fusion, no reranker; plan §10).
            adapted = await ExtractionService._gather_category_evidence(
                extraction, session, settings
            )
            if not adapted:
                raise ValidationError(
                    "No embedded chunks available for this document version."
                )

            # 2. Union/dedupe + budgeted SOURCE context (build_context reused
            #    verbatim — its dedup_sources collapses cross-category
            #    duplicate chunks; plan §16 risk table).
            bundle = build_context(
                adapted, budget_tokens=settings.extraction_context_token_budget
            )

            provider = get_llm_provider()
            if provider is None:
                raise RuntimeError(
                    "No LLM provider configured — extraction unavailable."
                )

            # 3. Draft (one constrained-JSON call + one parse retry).
            draft, model, usage = await generate_extraction_draft(
                bundle,
                provider,
                max_items_per_category=settings.extraction_max_items_per_category,
            )

            # 4. Flatten → resolve → validate → [bounded regen] → map back.
            items_by_category = await ExtractionService._draft_to_validated_items(
                bundle, draft,
                regenerate_once=lambda: generate_extraction_draft(
                    bundle,
                    provider,
                    max_items_per_category=(
                        settings.extraction_max_items_per_category
                    ),
                    extra_instruction=_citation_emphasis_instruction(),
                ),
            )

            # 5. Bulk-insert surviving items; run → COMPLETED.
            rows = ExtractionService._build_item_rows(items_by_category)
            await repo.add_items(extraction.id, rows)
            await repo.update_status(
                extraction,
                "COMPLETED",
                model=model,
                prompt_version=EXTRACTION_PROMPT_VERSION,
                prompt_tokens=usage.get("prompt_tokens") if usage else None,
                completion_tokens=usage.get("completion_tokens") if usage else None,
            )
            await session.commit()
            logger.info(
                "Extraction %s COMPLETED (job=%s): items=%d sources=%d",
                extraction.id, getattr(job, "id", "?"), len(rows), len(bundle.blocks),
            )
        except Exception:
            await ExtractionService._mark_failed_safe(repo, extraction, session)
            raise

    # ── Pipeline internals ────────────────────────────────────────────────────

    @staticmethod
    async def _gather_category_evidence(
        extraction: DocumentExtraction,
        session: AsyncSession,
        settings: Any,
    ) -> list[SearchResult]:
        """Per-category semantic retrieval → deduped SearchResult list (§5.9 step 1–2).

        Calls DocumentChunkRepository.semantic_search DIRECTLY — the same
        repository method ConflictService.generate_candidates_for_chunk
        already calls — bypassing HybridRetriever's multi-document
        scope-resolution layer because the target version is already known
        and already authorized (no ambiguity to resolve).
        """
        chunk_repo = DocumentChunkRepository(session)
        embedding_provider = get_embedding_provider()
        if embedding_provider is None:
            raise RuntimeError(
                "No embedding provider configured — extraction unavailable."
            )

        by_chunk: dict[str, tuple[ChunkSearchResult, float]] = {}
        for category in CATEGORIES:
            query = CATEGORY_QUERIES[category]
            vectors = await embedding_provider.embed([query])
            if not vectors:
                continue
            results = await chunk_repo.semantic_search(
                organization_id=extraction.organization_id,
                version_ids=[extraction.document_version_id],
                query_vector=list(vectors[0]),
                top_k=settings.extraction_top_k_per_category,
            )
            for result in results:
                existing = by_chunk.get(result.chunk_id)
                # A chunk may legitimately surface under several categories —
                # keep its best similarity once (dedupe across categories).
                if existing is None or result.similarity > existing[1]:
                    by_chunk[result.chunk_id] = (result, result.similarity)

        adapted: list[SearchResult] = []
        for position, (result, _similarity) in enumerate(by_chunk.values()):
            adapted.append(
                SearchResult(
                    chunk_id=result.chunk_id,
                    document_id=result.document_id,
                    document_version_id=result.document_version_id,
                    document_name=result.document_name,
                    page_id=result.page_id,
                    page_number=result.page_number,
                    section_title=result.section_title,
                    chunk_index=result.chunk_index,
                    snippet=(result.content or "")[:500],
                    content=result.content or "",
                    token_count=result.token_count,
                    relevance=result.similarity,
                    embedding_model=result.embedding_model,
                    metadata=result.metadata or {},
                )
            )
        return adapted

    @staticmethod
    async def _draft_to_validated_items(
        bundle: ContextBundle,
        draft: ExtractionDraft,
        *,
        regenerate_once: Callable[[], Awaitable[tuple[ExtractionDraft, Any, Any]]],
    ) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        """Flatten → resolve → validate → map back to (category, item) pairs.

        Identical reuse pattern to SummaryService steps 6–9, applied to four
        flat lists instead of six (plan §5.9 step 4).  Returns per category
        the surviving (clean_label, citation_dict) pairs in order.
        """
        flat_items, flattened = ExtractionService._flatten(draft)
        extraction_cit = resolve_citations(flattened, bundle)
        validation = await validate_answer(extraction_cit)

        if validation.should_regenerate:
            logger.info(
                "ExtractionService: central citation failure (%s) — one bounded "
                "regeneration",
                validation.regeneration_reason,
            )
            draft, _model, _usage = await regenerate_once()
            flat_items, flattened = ExtractionService._flatten(draft)
            extraction_cit = resolve_citations(flattened, bundle)
            validation = await validate_answer(extraction_cit, allow_regenerate=False)

        return ExtractionService._map_to_surviving(
            flat_items, extraction_cit, validation
        )

    @staticmethod
    def _flatten(draft: ExtractionDraft) -> tuple[list[_FlatItem], str]:
        """Four flat lists → one ordered pseudo-answer (§5.9 step 4)."""
        flat_items: list[_FlatItem] = []
        parts: list[str] = []
        for category in CATEGORIES:
            for index, text in enumerate(draft.items_for(category)):
                if not text.strip():
                    continue
                flat_items.append(
                    _FlatItem(category=category, item_index=index, text=text)
                )
                parts.append(text)
        return flat_items, "\n".join(parts)

    @staticmethod
    def _map_to_surviving(
        flat_items: list[_FlatItem],
        extraction_cit: Any,
        validation: Any,
    ) -> dict[str, list[tuple[str, dict[str, Any]]]]:
        """Surviving (clean_label, citation_dict) pairs per category.

        Claim matching is BY SENTENCE TEXT (each flattened item IS one
        sentence), identical to SummaryService._build_payload's machinery.
        """
        claim_status: dict[str, str] = {
            _norm(claim.text): claim.status for claim in validation.claims
        }
        claim_norms = list(claim_status.keys())
        citations_by_claim: dict[str, list[ResolvedCitation]] = {}
        for citation in extraction_cit.citations:
            citations_by_claim.setdefault(_norm(citation.claim_text), []).append(
                citation
            )

        surviving: dict[str, list[tuple[str, dict[str, Any]]]] = {
            category: [] for category in CATEGORIES
        }
        for item in flat_items:
            key = _norm(item.text)
            status = claim_status.get(key)
            if status is None:
                # Degenerate text: containment fallback — the item survives
                # only when every claim containing it survived.
                containing = [c for c in claim_norms if key and key in c]
                if not containing:
                    continue
                if any(
                    claim_status[c] in ("uncited", "unsupported")
                    for c in containing
                ):
                    continue
                citations = [
                    citation
                    for c in containing
                    for citation in citations_by_claim.get(c, [])
                ]
            else:
                if status in ("uncited", "unsupported"):
                    continue
                citations = citations_by_claim.get(key, [])
            if not citations:
                continue  # a persisted item always carries real provenance
            primary = citations[0]
            surviving[item.category].append(
                (
                    _strip_markers(item.text),
                    _citation_to_dict(primary),
                )
            )
        return surviving

    @staticmethod
    def _build_item_rows(
        items_by_category: dict[str, list[tuple[str, dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """Persistence rows with full citation-shaped provenance (§4.4)."""
        rows: list[dict[str, Any]] = []
        for category in CATEGORIES:
            for item_index, (label, citation) in enumerate(
                items_by_category.get(category, [])
            ):
                rows.append(
                    {
                        "category": category,
                        "item_index": item_index,
                        "label": label,
                        "detail": {"source_index": citation["index"]},
                        "document_id": citation["document_id"],
                        "document_version_id": citation["document_version_id"],
                        "chunk_id": citation["chunk_id"],
                        "page_id": citation["page_id"],
                        "page_number": citation["page_number"],
                        "section": citation["section"],
                        "quoted_text": citation["quoted_text"],
                        "char_start": citation["char_start"],
                        "char_end": citation["char_end"],
                        "relevance_score": citation["relevance"],
                    }
                )
        return rows

    @staticmethod
    async def _mark_failed_safe(
        repo: DocumentExtractionRepository,
        extraction: DocumentExtraction,
        session: AsyncSession,
    ) -> None:
        """Mark FAILED best-effort — never masks the original exception."""
        import sqlalchemy.exc

        try:
            await repo.update_status(
                extraction,
                "FAILED",
                error_message="Extraction run failed — see job record.",
            )
            await session.commit()
        except sqlalchemy.exc.SQLAlchemyError:
            logger.exception(
                "ExtractionService: FAILED transition also failed for run %s",
                extraction.id,
            )
            await session.rollback()


def _citation_emphasis_instruction() -> str:
    """The Phase 10 citation-emphasis instruction, adapted to the JSON draft."""
    from app.rag.prompts import CITATION_EMPHASIS_INSTRUCTION

    return (
        CITATION_EMPHASIS_INSTRUCTION
        + "\nRespond again with ONLY the required JSON object — every item in "
        "every category must end with a [N] citation marker."
    )


def _strip_markers(text: str) -> str:
    """Clean display text (citation markers removed)."""
    return _CITATION_MARKER_RE.sub("", text).strip()


def _norm(text: str) -> str:
    """Normalization for claim/citation sentence matching."""
    return " ".join(_strip_markers(text).split()).lower()


def _citation_to_dict(citation: ResolvedCitation) -> dict[str, Any]:
    """Citation fields for item-row provenance (§4.4 column mapping)."""
    return {
        "index": citation.index,
        "chunk_id": citation.chunk_id,
        "document_id": citation.document_id,
        "document_version_id": citation.document_version_id,
        "page_id": citation.page_id,
        "page_number": citation.page_number,
        "section": citation.section,
        "relevance": citation.relevance,
        "quoted_text": citation.quoted.text,
        "char_start": citation.quoted.char_start,
        "char_end": citation.quoted.char_end,
    }
