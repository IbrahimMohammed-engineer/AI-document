"""
Summary test fixtures (Phase 14, plan §8).

Seeds a READY document version with controlled sections + chunks so the
sampling algorithm and the summary pipeline are deterministic:

  - "Employee Handbook" — three top-level sections (Introduction, Leave
    Policy, Discipline), each with directly tagged chunks; plus a long
    variant whose total tokens exceed the summary budget (long-document
    sampling path).

Embeddings are NOT required for summaries (sampling is structural, not
vector-based — plan §2.6 point 7).

The smart LLM stub answers per prompt type: the analyzer gets a SUMMARY
classification, the summary builder gets a well-formed constrained JSON
draft whose items cite real SOURCE blocks.
"""
from __future__ import annotations

import hashlib
import json
import uuid

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


def _uuid() -> str:
    return str(uuid.uuid4())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def seed_summary_org(
    factory: async_sessionmaker,
    *,
    slug: str | None = None,
) -> tuple[Organization, User]:
    """Org + admin user (system Admin role) for summary tests."""
    slug = slug or f"summary-{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name=f"Summary Test Org {slug}", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Summary Admin",
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


async def seed_summary_document(
    factory: async_sessionmaker,
    *,
    organization_id: str,
    owner_id: str,
    name: str = "Employee Handbook",
    sections: list[dict] | None = None,
    status: str = "READY",
    set_current: bool = True,
) -> dict:
    """One document + READY version + page + sections/chunks.

    ``sections``: [{title, number, content, tokens?}, ...] — one top-level
    section + one directly-tagged chunk per entry, reading order preserved.

    Returns {document_id, version_id, page_id, section_ids, chunk_ids}.
    """
    sections = sections or [
        {"title": "Introduction", "number": "1", "content": "This handbook introduces company policy. [source 1]"},
        {"title": "Leave Policy", "number": "2", "content": "Employees receive twenty days of annual leave each year. Approvals require one week notice."},
        {"title": "Discipline", "number": "3", "content": "Violations of policy lead to progressive discipline up to termination."},
    ]

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
            version_number=1,
            storage_key=f"seed/{document.id}/v1.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            created_by=owner_id,
        )
        version.status = status
        await session.flush()

        page_id = _uuid()
        session.add(DocumentPage(
            id=page_id, document_version_id=version.id,
            page_number=1, text="\n".join(s["content"] for s in sections),
        ))
        await session.flush()

        section_ids: list[str] = []
        chunk_ids: list[str] = []
        chunk_index = 0
        for order, spec in enumerate(sections):
            section_id = _uuid()
            section_ids.append(section_id)
            session.add(DocumentSection(
                id=section_id,
                document_version_id=version.id,
                title=spec["title"],
                section_number=spec.get("number"),
                start_page=1,
                end_page=1,
                sort_order=order,
            ))
            content = spec["content"]
            chunk_id = _uuid()
            chunk_ids.append(chunk_id)
            session.add(DocumentChunk(
                id=chunk_id,
                organization_id=organization_id,
                document_version_id=version.id,
                page_id=page_id,
                section_id=section_id,
                chunk_index=chunk_index,
                content=content,
                content_hash=_sha256(content),
                token_count=spec.get("tokens", max(1, len(content.split()))),
            ))
            chunk_index += 1
        await session.flush()

        if set_current:
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

def summary_llm_responder(document_name: str = "Employee Handbook") -> object:
    """Message-aware stub answering each Phase 9–14 prompt type correctly.

    The summary draft cites SOURCE 1..3 — the fixture's three chunks — so
    every item resolves and survives validation (entailment is disabled or
    answered 'yes' by the stub's entailment branch).
    """

    def _responder(messages) -> str:
        system = messages[0].content if messages else ""

        if "classify a user's question" in system:  # query analyzer
            return json.dumps({
                "intent": "SUMMARY",
                "temporal_scope": None,
                "scope_hints": [],
                "topic": "policy_summary",
            })
        if "produce a structured summary" in system:  # summary builder
            return json.dumps({
                "executive_summary": f"{document_name} sets out company policy. [1]",
                "key_points": [
                    "Employees receive twenty days of annual leave. [2]",
                    "Policy violations lead to progressive discipline. [3]",
                ],
                "dates": [],
                "roles": ["Managers approve leave requests. [2]"],
                "requirements": ["Leave requests require one week notice. [2]"],
                "risks": ["Violations may lead to termination. [3]"],
                "topics": ["leave", "discipline"],
            })
        if "verify whether a source passage supports" in system:  # entailment
            return json.dumps({"verdict": "yes", "reason": "stated"})
        if "narrate a list of already-detected document conflicts" in system:
            return "No conflicts."
        return "Based on the provided sources, the documents describe the requested topic. [1]"

    return _responder


def install_summary_llm_stub(document_name: str = "Employee Handbook") -> StubLLMProvider:
    provider = StubLLMProvider(responder=summary_llm_responder(document_name))
    set_llm_provider(provider)
    return provider


async def mark_summary_completed(
    session: AsyncSession,
    summary_id: str,
    payload: dict,
) -> None:
    """Force a summary row COMPLETED with a payload (chat-reuse tests)."""
    await session.execute(
        text(
            "UPDATE document_summaries SET status = 'COMPLETED', "
            "summary = CAST(:payload AS jsonb), completed_at = now() "
            "WHERE id = :id"
        ),
        {"payload": json.dumps(payload), "id": summary_id},
    )
    await session.commit()
