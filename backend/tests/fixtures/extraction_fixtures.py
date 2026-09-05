"""
Extraction test fixtures (Phase 14, plan §8).

Seeds the roadmap's exit-criterion fixture: a contract document (READY,
single CURRENT version) with controlled pgvector embeddings so the four
category retrievals surface the intended evidence chunks deterministically
(the same raw-SQL CAST embedding pattern the conflict fixtures use).

Embedding layout (unit vectors on distinct axes):
  - requirement chunk: axis 10
  - risk chunk:        axis 20
  - date chunk:        axis 30
  - party chunk:       axis 40
The category queries embed to deterministic stub vectors; semantic search
ranks the axis-matching chunk first when the stub embedding provider maps
the query text to the matching axis.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.models.document import (
    DocumentChunk,
    DocumentPage,
    DocumentSection,
)
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.user_repository import OrganizationRepository, UserRepository

DIMENSIONS = 1536

CONTRACT_REQUIREMENT_TEXT = (
    "The vendor must deliver quarterly compliance reports within ten days "
    "of each quarter's end."
)
CONTRACT_RISK_TEXT = (
    "Late delivery incurs a penalty of five percent of the monthly fee."
)
CONTRACT_DATE_TEXT = "This agreement takes effect on January 1, 2026, and expires December 31, 2027."
CONTRACT_PARTY_TEXT = "Acme Corporation is the vendor; Northwind LLC is the customer."


def _uuid() -> str:
    return str(uuid.uuid4())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unit_vector(value_at: int) -> list[float]:
    vec = [0.0] * DIMENSIONS
    vec[value_at % DIMENSIONS] = 1.0
    return vec


async def set_chunk_embedding(
    session: AsyncSession, chunk_id: str, vector: Sequence[float]
) -> None:
    """Write one chunk's embedding via the raw-SQL cast pattern."""
    vector_str = "[" + ",".join(str(x) for x in vector) + "]"
    await session.execute(
        text(
            "UPDATE document_chunks "
            "SET embedding = CAST(:vec AS vector), embedding_model = 'stub-embed' "
            "WHERE id = :id"
        ),
        {"vec": vector_str, "id": chunk_id},
    )


async def seed_extraction_org(
    factory: async_sessionmaker,
    *,
    slug: str | None = None,
) -> tuple[Organization, User]:
    """Org + admin user (system Admin role) for extraction tests."""
    slug = slug or f"extraction-{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name=f"Extraction Test Org {slug}", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Extraction Admin",
            password_hash="$argon2id$test",
        )
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


async def seed_contract_document(
    factory: async_sessionmaker,
    *,
    organization_id: str,
    owner_id: str,
    name: str = "Service Agreement 2026",
    with_embeddings: bool = True,
) -> dict:
    """Contract document with one section + chunk per category."""
    specs = [
        {"title": "Obligations", "number": "2", "content": CONTRACT_REQUIREMENT_TEXT, "axis": 10},
        {"title": "Penalties", "number": "5", "content": CONTRACT_RISK_TEXT, "axis": 20},
        {"title": "Term", "number": "3", "content": CONTRACT_DATE_TEXT, "axis": 30},
        {"title": "Parties", "number": "1", "content": CONTRACT_PARTY_TEXT, "axis": 40},
    ]

    async with factory() as session:
        doc_repo = DocumentRepository(session)
        document = await doc_repo.create(
            organization_id=organization_id,
            owner_id=owner_id,
            name=name,
            document_type="contract",
        )
        ver_repo = DocumentVersionRepository(session)
        version = await ver_repo.create(
            document_id=document.id,
            version_number=1,
            storage_key=f"seed/{document.id}/v1.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            created_by=owner_id,
        )
        version.status = "READY"
        await session.flush()

        page_id = _uuid()
        session.add(DocumentPage(
            id=page_id, document_version_id=version.id,
            page_number=1, text="\n".join(s["content"] for s in specs),
        ))
        await session.flush()

        section_ids: list[str] = []
        chunk_ids: list[str] = []
        chunk_index = 0
        for order, spec in enumerate(specs):
            section_id = _uuid()
            section_ids.append(section_id)
            session.add(DocumentSection(
                id=section_id,
                document_version_id=version.id,
                title=spec["title"],
                section_number=spec["number"],
                start_page=1,
                end_page=1,
                sort_order=order,
            ))
            chunk_id = _uuid()
            chunk_ids.append(chunk_id)
            session.add(DocumentChunk(
                id=chunk_id,
                organization_id=organization_id,
                document_version_id=version.id,
                page_id=page_id,
                section_id=section_id,
                chunk_index=chunk_index,
                content=spec["content"],
                content_hash=_sha256(spec["content"]),
                token_count=max(1, len(spec["content"].split())),
            ))
            if with_embeddings:
                await set_chunk_embedding(session, chunk_id, _unit_vector(spec["axis"]))
            chunk_index += 1
        await session.flush()

        document.current_version_id = version.id
        await session.commit()
        return {
            "document_id": document.id,
            "version_id": version.id,
            "page_id": page_id,
            "section_ids": section_ids,
            "chunk_ids": chunk_ids,
        }


# ── Smart LLM stub ────────────────────────────────────────────────────────────

def extraction_llm_responder() -> object:
    """Message-aware stub answering the extraction prompt with a valid draft
    whose items cite SOURCE 1..N (the four fixture chunks)."""

    def _responder(messages) -> str:
        system = messages[0].content if messages else ""

        if "classify a user's question" in system:  # query analyzer
            return json.dumps({
                "intent": "EXTRACTION",
                "temporal_scope": None,
                "scope_hints": [],
                "topic": "contract_terms",
            })
        if "extract structured information" in system:  # extraction builder
            return json.dumps({
                "requirement": ["Quarterly compliance reports due in ten days. [1]"],
                "risk": ["Late delivery incurs a five percent penalty. [2]"],
                "date": ["Agreement effective January 1, 2026. [3]"],
                "party": ["Acme Corporation is the vendor. [4]"],
            })
        if "verify whether a source passage supports" in system:  # entailment
            return json.dumps({"verdict": "yes", "reason": "stated"})
        if "narrate a list of already-extracted document items" in system:
            return "Here is what the contract contains, grouped by category."
        return "Based on the provided sources, the documents describe the requested topic. [1]"

    return _responder


def install_extraction_llm_stub() -> StubLLMProvider:
    provider = StubLLMProvider(responder=extraction_llm_responder())
    set_llm_provider(provider)
    return provider
