"""
Conflict test fixtures (Phase 13, plan §27.7).

Seeds the roadmap's exit-criterion fixture: two documents, both with a
single CURRENT, READY version, plus controlled pgvector embeddings so
candidate generation is deterministic:

  - "HR Policy A" — section "Vacation Approval":
        "Vacation requests must be approved by the employee's direct manager."
  - "HR Policy B" — section "Leave Procedures":
        "All vacation requests require HR department approval before submission."

Embeddings are written directly (raw SQL CAST, the same pattern the
embedding stage uses) with hand-controlled vectors:
  - the two conflicting statements point 0.9 cosine apart-from-orthogonal —
    similarity 0.9 >= the 0.83 candidate threshold;
  - decoy vectors (orthogonal) stay far below the threshold.

The smart LLM stub answers per prompt type: the analyzer gets a
CONFLICT_DETECTION classification, the contradiction-check call gets a
confirmed conflict, the narration calls phrase their structured inputs.
"""
from __future__ import annotations

import json
import uuid
from datetime import date as _date
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.models.document import (
    DocumentChunk,
    DocumentPage,
    DocumentSection,
    DocumentVersion,
)
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.user_repository import OrganizationRepository, UserRepository

DIMENSIONS = 1536

HR_POLICY_A_TEXT = (
    "Vacation requests must be approved by the employee's direct manager."
)
HR_POLICY_B_TEXT = (
    "All vacation requests require HR department approval before submission."
)


# ── Controlled embedding vectors ──────────────────────────────────────────────

def _unit_vector(value_at: int) -> list[float]:
    """A unit vector with 1.0 at position ``value_at`` (else 0.0)."""
    vec = [0.0] * DIMENSIONS
    vec[value_at % DIMENSIONS] = 1.0
    return vec


def _pair_vector(primary: int, secondary: int, cos: float = 0.9) -> list[float]:
    """Unit vector at ``cos`` cosine similarity with ``_unit_vector(primary)``."""
    sin = (1.0 - cos * cos) ** 0.5
    vec = [0.0] * DIMENSIONS
    vec[primary % DIMENSIONS] = cos
    vec[secondary % DIMENSIONS] = sin
    return vec


async def set_chunk_embedding(
    session: AsyncSession, chunk_id: str, vector: Sequence[float]
) -> None:
    """Write one chunk's embedding via the raw-SQL cast pattern (Phase 7's)."""
    vector_str = "[" + ",".join(str(x) for x in vector) + "]"
    await session.execute(
        text(
            "UPDATE document_chunks "
            "SET embedding = CAST(:vec AS vector), embedding_model = 'stub-embed' "
            "WHERE id = :id"
        ),
        {"vec": vector_str, "id": chunk_id},
    )


# ── Seeding ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _sha256(text_value: str) -> str:
    import hashlib

    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


async def seed_conflict_document(
    factory: async_sessionmaker,
    *,
    organization_id: str,
    owner_id: str,
    name: str,
    section_number: str,
    section_title: str,
    content: str,
    embedding: Sequence[float] | None = None,
    version_number: int = 1,
    effective_date=None,
    status: str = "READY",
) -> dict:
    """One document + one version + one page + one section/chunk.

    Returns {document_id, version_id, page_id, section_id, chunk_id}.
    """
    async with factory() as session:
        doc_repo = DocumentRepository(session)
        document = await doc_repo.create(
            organization_id=organization_id,
            owner_id=owner_id,
            name=name,
            document_type="policy",
        )
        ver_repo = DocumentVersionRepository(session)
        version = await ver_repo.create(
            document_id=document.id,
            version_number=version_number,
            storage_key=f"seed/{document.id}/v{version_number}.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            created_by=owner_id,
        )
        version.status = status
        if effective_date is not None:
            version.effective_date = effective_date
        await session.flush()

        page_id = _uuid()
        session.add(DocumentPage(
            id=page_id,
            document_version_id=version.id,
            page_number=1,
            text=content,
        ))
        await session.flush()

        section_id = _uuid()
        session.add(DocumentSection(
            id=section_id,
            document_version_id=version.id,
            title=section_title,
            section_number=section_number,
            start_page=1,
            end_page=1,
            sort_order=0,
        ))
        chunk_id = _uuid()
        session.add(DocumentChunk(
            id=chunk_id,
            organization_id=organization_id,
            document_version_id=version.id,
            page_id=page_id,
            section_id=section_id,
            chunk_index=0,
            content=content,
            content_hash=_sha256(content),
            token_count=max(1, len(content.split())),
        ))
        await session.flush()

        if embedding is not None:
            await set_chunk_embedding(session, chunk_id, embedding)

        document.current_version_id = version.id
        await session.commit()
        return {
            "document_id": document.id,
            "version_id": version.id,
            "page_id": page_id,
            "section_id": section_id,
            "chunk_id": chunk_id,
        }


async def seed_conflict_org(
    factory: async_sessionmaker,
    *,
    slug: str | None = None,
) -> tuple[Organization, User]:
    """Org + admin user (with the system Admin role) for conflict tests."""
    slug = slug or f"conflict-{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name=f"Conflict Test Org {slug}", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Conflict Admin",
            password_hash="$argon2id$test",
        )
        # Grant the system Admin role (seeded by migration 002/013) so the
        # user passes live conflict:resolve permission checks.
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_id, organization_id) "
                "SELECT :uid, r.id, :oid FROM roles r "
                "WHERE r.name = 'Admin' AND r.organization_id IS NULL "
                "ON CONFLICT DO NOTHING"
            ),
            {"uid": user.id, "oid": org.id},
        )
        await session.commit()
        return org, user


async def seed_conflict_corpus(
    factory: async_sessionmaker,
    *,
    slug: str | None = None,
) -> dict:
    """The §27.7 end-to-end fixture: org + user + two conflicting documents.

    Returns {org, user, policy_a: {...}, policy_b: {...}} with embedding
    vectors giving the pair 0.9 cosine similarity (above the 0.83 candidate
    threshold).
    """
    org, user = await seed_conflict_org(factory, slug=slug)
    policy_a = await seed_conflict_document(
        factory,
        organization_id=org.id,
        owner_id=user.id,
        name="HR Policy A",
        section_number="1",
        section_title="Vacation Approval",
        content=HR_POLICY_A_TEXT,
        embedding=_unit_vector(0),
        effective_date=_date(2025, 1, 1),
    )
    policy_b = await seed_conflict_document(
        factory,
        organization_id=org.id,
        owner_id=user.id,
        name="HR Policy B",
        section_number="2",
        section_title="Leave Procedures",
        content=HR_POLICY_B_TEXT,
        embedding=_pair_vector(0, 1, cos=0.9),
        effective_date=_date(2025, 1, 1),
    )
    return {"org": org, "user": user, "policy_a": policy_a, "policy_b": policy_b}


async def set_org_critical_sections(
    factory: async_sessionmaker, organization_id: str, patterns: list[str]
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE organizations SET settings = CAST(:s AS jsonb) "
                "WHERE id = :id"
            ),
            {"id": organization_id, "s": json.dumps(
                {"comparison": {"critical_sections": patterns}}
            )},
        )
        await session.commit()


# ── Smart LLM stub ────────────────────────────────────────────────────────────

def conflict_llm_responder(messages) -> str:
    """Message-aware stub answering each Phase 9–13 prompt type correctly."""
    system = messages[0].content if messages else ""

    if "classify a user's question" in system:  # query analyzer
        return json.dumps({
            "intent": "CONFLICT_DETECTION",
            "temporal_scope": None,
            "scope_hints": ["vacation"],
            "topic": "vacation_rules",
        })
    if "CONTRADICT" in system:  # contradiction check (§11)
        return json.dumps({
            "is_conflict": True,
            "confidence": 0.9,
            "reason": "The two documents name different approvers.",
            "conflict_topic": "Vacation Approval Authority",
        })
    if "narrate a list of already-detected document conflicts" in system:
        return (
            "Your documents disagree on vacation approval authority: HR "
            "Policy A requires manager approval while HR Policy B requires "
            "HR department approval."
        )
    if "classify whether the meaning of a document section changed" in system:
        return json.dumps({"materiality": "material", "rationale": "changed"})
    if "narrate a list of already-classified document changes" in system:
        return "Changes narrated."
    return "Based on the provided sources, the documents describe the requested topic. [1]"


def install_conflict_llm_stub() -> StubLLMProvider:
    provider = StubLLMProvider(responder=conflict_llm_responder)
    set_llm_provider(provider)
    return provider
