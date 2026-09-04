"""
Comparison test fixtures (Phase 12, plan §16.6).

Seeds the roadmap's exit-criterion fixture: a "Marketing Policy" document
with TWO READY versions —

  v1 ("2025", effective 2025-01-01):
    1. Purpose           — identical in both versions
    3.1 Approval Process — "Approval must be completed within 5 business days."
    4.2 Review Cycle     — "The review cycle occurs every 12 months."
  v2 ("2026", effective 2026-01-01):
    1. Purpose           — identical
    3.1 Approval Process — "Approval must be completed within 7 business days."
    4.2 Review Cycle     — "The review cycle occurs every 6 months."
    5.1 Digital Signatures — ADDED (exists only in v2)

Each section gets one page and one chunk (content_hash = sha256 of content)
so the worker's alignment (pass 1: section-number match), text diff, and
severity pipeline have realistic data.

Seeding uses real ORM models through the repositories where practical,
mirroring the existing test conventions (no factory_boy).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

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


@dataclass
class SectionSpec:
    """One section of one version."""

    section_number: str
    title: str
    content: str


@dataclass
class VersionSeed:
    """IDs of the rows created for one version."""

    version_id: str
    page_id: str
    sections: dict[str, str] = field(default_factory=dict)   # number → section id
    chunks: dict[str, str] = field(default_factory=dict)     # number → chunk id


@dataclass
class ComparisonSeed:
    """Everything the comparison tests need to address the fixture."""

    org: Organization
    user: User
    document_id: str
    v2025: VersionSeed
    v2026: VersionSeed

    @property
    def version_a_id(self) -> str:  # the "2025" side
        return self.v2025.version_id

    @property
    def version_b_id(self) -> str:  # the "2026" side
        return self.v2026.version_id


V1_SECTIONS = [
    SectionSpec("1", "Purpose", "This policy defines the marketing approval workflow."),
    SectionSpec("3.1", "Approval Process",
                "Approval must be completed within 5 business days."),
    SectionSpec("4.2", "Review Cycle",
                "The review cycle occurs every 12 months."),
]

V2_SECTIONS = [
    SectionSpec("1", "Purpose", "This policy defines the marketing approval workflow."),
    SectionSpec("3.1", "Approval Process",
                "Approval must be completed within 7 business days."),
    SectionSpec("4.2", "Review Cycle",
                "The review cycle occurs every 6 months."),
    SectionSpec("5.1", "Digital Signatures",
                "Digital signatures are accepted for all marketing approvals."),
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _seed_version(
    session: AsyncSession,
    *,
    organization_id: str,
    document_id: str,
    version_number: int,
    effective_date,
    created_by: str,
    sections: list[SectionSpec],
) -> VersionSeed:
    """One READY version + one page + one section/chunk per spec."""
    ver_repo = DocumentVersionRepository(session)
    version = await ver_repo.create(
        document_id=document_id,
        version_number=version_number,
        storage_key=f"seed/{document_id}/v{version_number}.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        created_by=created_by,
    )
    version.status = "READY"
    version.effective_date = effective_date
    await session.flush()

    seed = VersionSeed(version_id=version.id, page_id=_uuid())
    session.add(DocumentPage(
        id=seed.page_id,
        document_version_id=version.id,
        page_number=1,
        text="\n".join(s.content for s in sections),
    ))
    await session.flush()

    for order, spec in enumerate(sections):
        section_id = _uuid()
        seed.sections[spec.section_number] = section_id
        session.add(DocumentSection(
            id=section_id,
            document_version_id=version.id,
            title=spec.title,
            section_number=spec.section_number,
            start_page=1,
            end_page=1,
            sort_order=order,
        ))
        chunk_id = _uuid()
        seed.chunks[spec.section_number] = chunk_id
        session.add(DocumentChunk(
            id=chunk_id,
            organization_id=organization_id,
            document_version_id=version.id,
            page_id=seed.page_id,
            section_id=section_id,
            chunk_index=order,
            content=spec.content,
            content_hash=_sha256(spec.content),
            token_count=max(1, len(spec.content.split())),
        ))
    await session.flush()
    return seed


async def seed_document_with_versions(
    factory: async_sessionmaker,
    *,
    organization_id: str,
    owner_id: str,
    name: str = "Marketing Policy",
    versions: list[tuple[int, object, list[SectionSpec]]] | None = None,
) -> tuple[str, dict[int, VersionSeed]]:
    """Create a document + READY versions for an EXISTING org/user (API tests).

    ``versions`` entries are (version_number, effective_date, sections).
    Defaults to the §16.6 fixture pair.  Returns (document_id, {vno: seed}).
    """
    from datetime import date as _date

    if versions is None:
        versions = [
            (1, _date(2025, 1, 1), V1_SECTIONS),
            (2, _date(2026, 1, 1), V2_SECTIONS),
        ]
    async with factory() as session:
        doc_repo = DocumentRepository(session)
        document = await doc_repo.create(
            organization_id=organization_id,
            owner_id=owner_id,
            name=name,
            document_type="policy",
        )
        seeds: dict[int, VersionSeed] = {}
        for version_number, effective_date, sections in versions:
            seeds[version_number] = await _seed_version(
                session,
                organization_id=organization_id,
                document_id=document.id,
                version_number=version_number,
                effective_date=effective_date,
                created_by=owner_id,
                sections=sections,
            )
        document.current_version_id = seeds[max(seeds)].version_id
        await session.flush()
        await session.commit()
        return document.id, seeds


async def seed_comparison_document(
    factory: async_sessionmaker,
    *,
    slug: str | None = None,
    document_name: str = "Marketing Policy",
) -> ComparisonSeed:
    """Seed org + user + the two-version Marketing Policy fixture (commits)."""
    from datetime import date as _date

    slug = slug or f"compare-{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name=f"Compare Test Org {slug}", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Compare Admin",
            password_hash="$argon2id$test",
        )
        doc_repo = DocumentRepository(session)
        document = await doc_repo.create(
            organization_id=org.id,
            owner_id=user.id,
            name=document_name,
            document_type="policy",
        )

        v2025 = await _seed_version(
            session, organization_id=org.id, document_id=document.id,
            version_number=1, effective_date=_date(2025, 1, 1),
            created_by=user.id, sections=V1_SECTIONS,
        )
        v2026 = await _seed_version(
            session, organization_id=org.id, document_id=document.id,
            version_number=2, effective_date=_date(2026, 1, 1),
            created_by=user.id, sections=V2_SECTIONS,
        )
        document.current_version_id = v2026.version_id
        await session.flush()
        await session.commit()
        return ComparisonSeed(
            org=org, user=user, document_id=document.id,
            v2025=v2025, v2026=v2026,
        )


def install_material_llm_stub() -> StubLLMProvider:
    """LLM stub that answers the semantic-comparison call with 'material'.

    With materiality=material and proportion < 0.3, a two-word change is
    MODERATE (plan §9.8 worked example) — the severity the §16.6 fixture
    asserts for the approval-window change.
    """
    provider = StubLLMProvider(
        responder=lambda messages: '{"materiality": "material", "rationale": "The deadline changed."}'
    )
    set_llm_provider(provider)
    return provider
