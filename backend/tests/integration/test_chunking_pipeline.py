"""
Integration tests for the Phase 6 structure-detection + chunking pipeline.

Runs against testcontainers PostgreSQL (full Alembic stack incl.
document_sections/document_chunks) + Redis, mirroring the Phase 5 harness:

  - EXTRACTION chains a CHUNKING job (durable row + pointer, Backend §50)
  - structured PDF → section tree + chunks with correct provenance FKs
    (version / page(s) / section), content hashes, token counts, and the
    DB-generated content_tsv populated
  - table metadata flags (contains_table) with whole-row integrity
  - no-structure documents (plain + OCR'd scans) → zero sections, chunks
    still produced (explicitly supported, non-error — Backend §20)
  - re-run → UPSERT overwrite, never duplicates (Backend §49)
  - trg_chunk_org_consistency trigger rejects a cross-org chunk write
    (DB §28 — the Phase 6 exit-criteria demonstration)
  - GET /documents/{id}/toc returns the section tree ("No structure
    detected" is an empty items list, FE §6.5)
  - GET /documents/{id}/chunks returns chunks with provenance (debug tooling)

The Arq worker loop is not started — run_processing_job is invoked directly
against real PostgreSQL/Redis (exactly what the worker executes per pointer).

Requires Docker (testcontainers). Tesseract is NOT required.
"""
from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.ocr import OCRResult, PageContext
from app.infrastructure.storage import (
    ObjectStorageProvider,
    StorageObjectMissingError,
)
from app.models.document import Document, DocumentVersion
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.document_page_repository import DocumentPageRepository
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.document_section_repository import (
    DocumentSectionRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.repositories.user_repository import OrganizationRepository, UserRepository
from app.workers.jobs import run_processing_job

# FK-safe delete order for the tables these tests touch. Citations/messages
# come FIRST: citations RESTRICT-delete against cited chunks (Phase 10);
# feedback/conversations precede users (Phase 11 FK policy).
_TABLES_TO_CLEAN = (
    "message_feedback",
    "citations",
    "messages",
    "conversation_documents",
    "conversations",
    "document_chunks",
    "document_sections",
    "document_pages",
    "processing_jobs",
    "collection_documents",
    "document_tags",
    "collections",
    "document_versions",
    "documents",
    "audit_logs",
    "refresh_tokens",
    "user_roles",
    "users",
    "organizations",
)


# ─── Fixtures (harness parity with Phase 4/5) ─────────────────────────────────

async def _clean_tables(factory: async_sessionmaker) -> None:
    async with factory() as session:
        for table in _TABLES_TO_CLEAN:
            await session.execute(text(f'DELETE FROM "{table}"'))
        await session.commit()


@pytest_asyncio.fixture()
async def worker_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> AsyncGenerator[async_sessionmaker, None]:
    """Bind worker/queue infrastructure to the test containers."""
    await _clean_tables(app_session_factory)

    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)

    yield app_session_factory

    await _clean_tables(app_session_factory)
    await pool.aclose()


class FakeStorageProvider(ObjectStorageProvider):
    """In-memory ObjectStorageProvider — mirrors the real interface."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, key, data, content_type="application/octet-stream") -> str:
        self.objects[key] = data
        return key

    async def upload_stream(self, key, data_iter, content_type="application/octet-stream") -> str:
        chunks = []
        async for chunk in data_iter:
            chunks.append(chunk)
        return await self.upload(key, b"".join(chunks), content_type)

    async def download(self, key) -> bytes:
        if key not in self.objects:
            raise StorageObjectMissingError(f"missing: {key}")
        return self.objects[key]

    async def generate_signed_url(self, key, expires_in_seconds=900) -> str:
        return f"https://fake-storage.local/{key}"

    async def delete(self, key) -> None:
        self.objects.pop(key, None)

    async def exists(self, key) -> bool:
        return key in self.objects

    async def stat(self, key) -> dict:
        if key not in self.objects:
            raise StorageObjectMissingError(f"Object not found in storage: {key}")
        return {"size": len(self.objects[key]), "content_type": "application/pdf"}


@pytest_asyncio.fixture()
async def fake_storage(monkeypatch) -> FakeStorageProvider:
    provider = FakeStorageProvider()
    import app.infrastructure.storage as storage_mod

    monkeypatch.setattr(storage_mod, "_storage_provider", provider)
    return provider


class FakeOCRProvider:
    """Deterministic OCRProvider stand-in (same as the Phase 5 harness)."""

    name = "fake-ocr"

    def __init__(self, text: str = "FAKE OCR TEXT FROM SCAN") -> None:
        self.text = text
        self.calls: list[int] = []

    async def recognize(self, image: bytes, page_context: PageContext) -> OCRResult:
        self.calls.append(page_context.page_number)
        return OCRResult(
            text=self.text,
            confidence=90.0,
            lines=(),
            provider=self.name,
        )


@pytest_asyncio.fixture()
async def fake_ocr(monkeypatch) -> FakeOCRProvider:
    provider = FakeOCRProvider()
    import app.workers.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "get_ocr_provider", lambda: provider)
    return provider


# ─── PDF fixture builders ─────────────────────────────────────────────────────

def _make_structured_pdf(pages: list[list[tuple[str, int]]]) -> bytes:
    """Build a PDF from (text, fontsize) lines per page via PyMuPDF."""
    import fitz

    doc = fitz.open()
    for page_lines in pages:
        page = doc.new_page()
        y = 72
        for line_text, fontsize in page_lines:
            page.insert_text((72, y), line_text, fontsize=fontsize)
            y += int(fontsize * 1.6) + 6
    data = doc.tobytes()
    doc.close()
    return data


def _make_plain_pdf(page_texts: list[str]) -> bytes:
    import fitz

    doc = fitz.open()
    for text_content in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text_content, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _make_scanned_pdf(page_count: int) -> bytes:
    import fitz
    from PIL import Image

    doc = fitz.open()
    for _ in range(page_count):
        page = doc.new_page()
        buf = Image.new("RGB", (400, 300), color="#dddddd")
        import io

        image_bytes = io.BytesIO()
        buf.save(image_bytes, format="PNG")
        page.insert_image(
            fitz.Rect(100, 100, 500, 400), stream=image_bytes.getvalue()
        )
    data = doc.tobytes()
    doc.close()
    return data


# ─── Document fixtures ────────────────────────────────────────────────────────

async def _make_document_tree(
    factory: async_sessionmaker,
    *,
    slug: str = "chunk-org",
    mime_type: str = "application/pdf",
) -> tuple[Organization, User, Document, DocumentVersion]:
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name="Chunk Test Org", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Chunk Admin",
            password_hash="$argon2id$test",
        )
        doc = await DocumentRepository(session).create(
            organization_id=org.id,
            owner_id=user.id,
            name="Phase 6 Chunking Document",
            document_type="policy",
        )
        version = await DocumentVersionRepository(session).create(
            document_id=doc.id,
            version_number=1,
            storage_key=f"organizations/{org.id}/documents/{doc.id}/versions/v1/original.pdf",
            mime_type=mime_type,
            file_size_bytes=0,
            created_by=user.id,
        )
        doc.current_version_id = version.id
        await session.flush()
        await session.commit()
        return org, user, doc, version


async def _make_job(
    factory: async_sessionmaker,
    *,
    organization_id: str,
    document_version_id: str,
    job_type: str = "EXTRACTION",
    max_attempts: int = 3,
) -> str:
    async with factory() as session:
        job = await ProcessingJobRepository(session).create(
            organization_id=organization_id,
            document_version_id=document_version_id,
            job_type=job_type,
            max_attempts=max_attempts,
        )
        await session.commit()
        return job.id


async def _get_job(factory: async_sessionmaker, job_id: str):
    async with factory() as session:
        job = await ProcessingJobRepository(session).get_by_id(job_id)
        assert job is not None
        session.expunge(job)
        return job


async def _get_version(factory: async_sessionmaker, version_id: str):
    async with factory() as session:
        version = await DocumentVersionRepository(session).get_by_id(version_id)
        assert version is not None
        session.expunge(version)
        return version


async def _get_pages(factory: async_sessionmaker, version_id: str):
    async with factory() as session:
        repo = DocumentPageRepository(session)
        pages = await repo.list_for_version(version_id)
        for p in pages:
            session.expunge(p)
        return pages


async def _get_sections(factory: async_sessionmaker, version_id: str):
    async with factory() as session:
        repo = DocumentSectionRepository(session)
        sections = await repo.list_for_version(version_id)
        for s in sections:
            session.expunge(s)
        return sections


async def _get_chunks(factory: async_sessionmaker, version_id: str):
    async with factory() as session:
        repo = DocumentChunkRepository(session)
        chunks = await repo.list_for_version(version_id)
        for c in chunks:
            session.expunge(c)
        return chunks


async def _pending_job_of_type(
    factory: async_sessionmaker, version_id: str, job_type: str
) -> str | None:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM processing_jobs "
                    "WHERE document_version_id = :v AND job_type = :t "
                    "  AND status = 'PENDING' LIMIT 1"
                ),
                {"v": version_id, "t": job_type},
            )
        ).fetchone()
        return str(row[0]) if row else None


async def _run_chain(
    factory: async_sessionmaker,
    organization_id: str,
    version_id: str,
) -> str:
    """Run EXTRACTION then the chained CHUNKING job. Returns final status."""
    extraction_job = await _make_job(
        factory, organization_id=organization_id, document_version_id=version_id
    )
    assert await run_processing_job({}, extraction_job) == "COMPLETED"

    chunking_job = await _pending_job_of_type(factory, version_id, "CHUNKING")
    assert chunking_job is not None, "extraction must chain a CHUNKING job"
    return await run_processing_job({}, chunking_job)


# ─── Structured document end-to-end ───────────────────────────────────────────

STRUCTURED_PAGES: list[list[tuple[str, int]]] = [
    [
        ("1. Purpose", 16),
        ("This policy defines the marketing approval process for communications.", 10),
        ("2. Scope", 16),
        ("Applies to every department that publishes content on our behalf.", 10),
    ],
    [
        ("4. Approval Process", 16),
        ("All campaigns require documented approval before any launch happens.", 10),
        ("4.1 Marketing Review", 14),
        ("The marketing director reviews creative assets for brand consistency.", 10),
        ("4.2 Regulatory Review", 14),
        ("The regulatory team verifies claims are substantiated by evidence.", 10),
    ],
]

TABLE_PAGE: list[list[tuple[str, int]]] = [
    [
        ("3. Inspection Matrix", 16),
        ("The matrix below defines the inspection frequency for equipment.", 10),
        ("Equipment | Frequency | Owner | Escalation", 10),
        ("Fire extinguishers | Monthly | Facilities | Week 1", 10),
        ("Eyewash stations | Monthly | Labs | Week 1", 10),
        ("Sprinkler system | Quarterly | Facilities | Week 2", 10),
        ("5. Records", 16),
        ("Completed checklists are filed within two days after inspection.", 10),
    ]
]


@pytest.mark.integration
async def test_structured_document_produces_sections_and_chunks(
    worker_env, fake_storage
):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_structured_pdf(STRUCTURED_PAGES + TABLE_PAGE)
    fake_storage.objects[version.storage_key] = pdf

    assert (
        await _run_chain(factory, org.id, version.id) == "COMPLETED"
    )

    version = await _get_version(factory, version.id)
    assert version.status == "CHUNKING"  # chain ends here until Phase 7

    # ── Section tree ──────────────────────────────────────────────────────
    sections = await _get_sections(factory, version.id)
    numbers = [s.section_number for s in sections]
    assert numbers == ["1", "2", "4", "4.1", "4.2", "3", "5"]
    by_number = {s.section_number: s for s in sections}
    assert by_number["4.1"].parent_section_id == by_number["4"].id
    assert by_number["4.2"].parent_section_id == by_number["4"].id
    assert by_number["1"].parent_section_id is None
    assert by_number["5"].end_page == 3  # last page of the document

    # ── Chunks: provenance FKs all resolve ────────────────────────────────
    chunks = await _get_chunks(factory, version.id)
    assert chunks
    pages = await _get_pages(factory, version.id)
    page_ids = {p.id for p in pages}
    page_numbers = {p.id: p.page_number for p in pages}
    section_ids = {s.id for s in sections}

    indexes = [c.chunk_index for c in chunks]
    assert indexes == list(range(len(indexes)))  # 0-based reading order

    for chunk in chunks:
        assert chunk.organization_id == org.id  # denormalized + trigger-consistent
        assert chunk.page_id in page_ids
        if chunk.end_page_id is not None:
            assert chunk.end_page_id in page_ids
            assert page_numbers[chunk.end_page_id] >= page_numbers[chunk.page_id]
        if chunk.section_id is not None:
            assert chunk.section_id in section_ids
        assert len(chunk.content_hash) == 64  # SHA-256 hex
        assert chunk.token_count > 0
        meta = chunk.chunk_metadata
        assert isinstance(meta.get("heading_path"), list)
        assert meta["contains_table"] in (True, False)
        assert meta["contains_list"] is False
        assert len(meta["page_span"]) == 2

    # Table content flagged; a table chunk keeps whole rows
    table_chunks = [c for c in chunks if c.chunk_metadata.get("contains_table")]
    assert table_chunks, "the inspection matrix must be flagged contains_table"
    for chunk in table_chunks:
        for line in chunk.content.split("\n"):
            if "|" in line:
                assert len(line.split("|")) == 4  # never split mid-row

    # Heading path breadcrumb for a nested section's chunks
    nested = [
        c for c in chunks
        if (c.chunk_metadata or {}).get("section_number") == "4.1"
    ]
    assert nested
    assert nested[0].chunk_metadata["heading_path"] == [
        "Approval Process", "Marketing Review",
    ]

    # content_tsv generated column is populated by the database itself
    async with factory() as session:
        filled = (
            await session.execute(
                text(
                    "SELECT count(*) FROM document_chunks "
                    "WHERE document_version_id = :v AND content_tsv IS NOT NULL"
                ),
                {"v": version.id},
            )
        ).scalar()
    assert filled == len(chunks)


# ─── No-structure documents ───────────────────────────────────────────────────

@pytest.mark.integration
async def test_no_structure_document_has_zero_sections(worker_env, fake_storage):
    """Unstructured content → zero sections, chunks still produced."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_plain_pdf([
        "The quarterly facilities review found the north entrance carpet worn out.",
        "Lighting in corridor two was replaced with efficient fixtures that week.",
    ])
    fake_storage.objects[version.storage_key] = pdf

    assert await _run_chain(factory, org.id, version.id) == "COMPLETED"

    assert await _get_sections(factory, version.id) == []
    chunks = await _get_chunks(factory, version.id)
    assert chunks  # chunking degrades to page-level, non-error
    for chunk in chunks:
        assert chunk.section_id is None
        assert (chunk.chunk_metadata or {}).get("heading_path") == []


@pytest.mark.integration
async def test_scanned_document_chunks_ocr_text_without_sections(
    worker_env, fake_storage, fake_ocr
):
    """OCR'd scanned pages chunk from the STORED OCR text; no TOC noise.

    The fake OCR text is a single repeated line — the furniture/solo-line
    guards keep it out of the section tree.
    """
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_scanned_pdf(2)
    fake_storage.objects[version.storage_key] = pdf

    assert await _run_chain(factory, org.id, version.id) == "COMPLETED"

    assert await _get_sections(factory, version.id) == []
    chunks = await _get_chunks(factory, version.id)
    assert chunks
    assert all("FAKE OCR TEXT FROM SCAN" in c.content for c in chunks)
    assert all(c.section_id is None for c in chunks)


# ─── Idempotent re-run ────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_chunking_rerun_overwrites_without_duplicates(worker_env, fake_storage):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_structured_pdf(STRUCTURED_PAGES)
    fake_storage.objects[version.storage_key] = pdf

    assert await _run_chain(factory, org.id, version.id) == "COMPLETED"
    sections_first = await _get_sections(factory, version.id)
    chunks_first = await _get_chunks(factory, version.id)

    # Re-run: create a fresh CHUNKING job (terminal jobs never re-execute)
    rerun_job = await _make_job(
        factory,
        organization_id=org.id,
        document_version_id=version.id,
        job_type="CHUNKING",
        max_attempts=1,
    )
    assert await run_processing_job({}, rerun_job) == "COMPLETED"

    sections_second = await _get_sections(factory, version.id)
    chunks_second = await _get_chunks(factory, version.id)

    assert len(sections_second) == len(sections_first)
    assert len(chunks_second) == len(chunks_first)
    indexes = [c.chunk_index for c in chunks_second]
    assert indexes == list(range(len(indexes)))  # no duplicates
    # Section rows were replaced (new IDs), and chunk FKs still resolve
    assert {s.id for s in sections_second}.isdisjoint(
        {s.id for s in sections_first}
    )
    for chunk in chunks_second:
        if chunk.section_id is not None:
            assert chunk.section_id in {s.id for s in sections_second}
    # Content hashes are stable across re-runs
    assert [c.content_hash for c in chunks_second] == [
        c.content_hash for c in chunks_first
    ]


# ─── Org-consistency trigger (DB §28) ─────────────────────────────────────────

@pytest.mark.integration
async def test_trigger_rejects_cross_org_chunk_write(worker_env, fake_storage):
    """Injecting a chunk whose organization_id ≠ the document's org must fail."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_plain_pdf(["A single page of plain content without structure."])
    fake_storage.objects[version.storage_key] = pdf
    assert await _run_chain(factory, org.id, version.id) == "COMPLETED"

    chunks = await _get_chunks(factory, version.id)
    assert chunks

    # A second organization attempts to claim the chunk (org drift attack)
    async with factory() as session:
        org_b = await OrganizationRepository(session).create(
            name="Rogue Org", slug="rogue-org"
        )
        org_b_id = org_b.id
        await session.commit()

    async with factory() as session:
        page_row = (
            await session.execute(
                text(
                    "SELECT id FROM document_pages "
                    "WHERE document_version_id = :v LIMIT 1"
                ),
                {"v": version.id},
            )
        ).fetchone()
        assert page_row is not None
        with pytest.raises(Exception) as exc_info:
            await session.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(organization_id, document_version_id, page_id, chunk_index,"
                    " content, content_hash, token_count) "
                    "VALUES (:org, :ver, :page, 999, 'stolen content', :hash, 2)"
                ),
                {
                    "org": org_b_id,
                    "ver": version.id,
                    "page": str(page_row[0]),
                    "hash": chunks[0].content_hash,
                },
            )
    message = str(exc_info.value)
    assert "organization_id" in message and "does not match" in message


# ─── TOC + chunks APIs ────────────────────────────────────────────────────────

REGISTER_BODY = {
    "org_name": "Toc Corp",
    "slug": "toc-corp",
    "email": "admin@toc-corp.com",
    "full_name": "Toc Admin",
    "password": "super-secret-1",
}


async def _auth_headers(client) -> dict[str, str]:
    response = await client.post("/auth/register", json=REGISTER_BODY)
    assert response.status_code == 201, response.text
    response = await client.post(
        "/auth/login",
        json={
            "email": REGISTER_BODY["email"],
            "password": REGISTER_BODY["password"],
            "org_slug": REGISTER_BODY["slug"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _tree_for_auth_org(factory: async_sessionmaker, name: str):
    """Create doc+version under the registered auth org; return ids+version.

    Assumes the auth-org registration already happened (call _auth_headers
    first) — the org row is looked up by the REGISTER_BODY slug.
    """
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT o.id, u.id FROM organizations o "
                    "JOIN users u ON u.organization_id = o.id "
                    "WHERE o.slug = :slug"
                ),
                {"slug": REGISTER_BODY["slug"]},
            )
        ).fetchone()
    assert row is not None, "auth org must be registered before this helper"
    org_id, user_id = str(row[0]), str(row[1])

    async with factory() as session:
        doc = await DocumentRepository(session).create(
            organization_id=org_id,
            owner_id=user_id,
            name=name,
            document_type="policy",
        )
        version = await DocumentVersionRepository(session).create(
            document_id=doc.id,
            version_number=1,
            storage_key=f"organizations/{org_id}/documents/{doc.id}/versions/v1/original.pdf",
            mime_type="application/pdf",
            file_size_bytes=0,
            created_by=user_id,
        )
        doc.current_version_id = version.id
        await session.flush()
        await session.commit()
        session.expunge(doc)
        session.expunge(version)
        return org_id, doc, version


@pytest.mark.integration
async def test_toc_api_returns_section_tree(app_client, worker_env, fake_storage):
    """app_client declared FIRST so its identity teardown runs LAST."""
    factory = worker_env

    # Register/login first — the auth org must exist before the tree helper
    headers = await _auth_headers(app_client)

    org_id, doc, version = await _tree_for_auth_org(factory, "Toc API Document")
    pdf = _make_structured_pdf(STRUCTURED_PAGES)
    fake_storage.objects[version.storage_key] = pdf
    assert await _run_chain(factory, org_id, version.id) == "COMPLETED"

    response = await app_client.get(f"/documents/{doc.id}/toc", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["document_id"] == doc.id
    assert payload["version_id"] == version.id
    assert payload["has_structure"] is True
    assert payload["section_count"] == 5

    # Tree shape: roots 1, 2, 4 — with 4.1/4.2 nested under 4
    items = payload["items"]
    assert [node["section_number"] for node in items] == ["1", "2", "4"]
    assert all(node["level"] == 1 for node in items)
    assert items[0]["children"] == []
    nested = items[2]["children"]
    assert [node["section_number"] for node in nested] == ["4.1", "4.2"]
    assert all(node["level"] == 2 for node in nested)
    assert nested[0]["start_page"] == 2

    # Version filter parameter accepted
    response = await app_client.get(
        f"/documents/{doc.id}/toc", params={"version": 1}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["section_count"] == 5

    # Unauthenticated access is rejected
    response = await app_client.get(f"/documents/{doc.id}/toc")
    assert response.status_code == 401

    # Unknown document is a 404, not an existence leak
    response = await app_client.get(
        "/documents/00000000-0000-0000-0000-000000000000/toc", headers=headers
    )
    assert response.status_code == 404


@pytest.mark.integration
async def test_toc_api_empty_items_is_no_structure_state(
    app_client, worker_env, fake_storage
):
    """Zero sections → has_structure=false + empty items (FE §6.5)."""
    factory = worker_env

    headers = await _auth_headers(app_client)

    org_id, doc, version = await _tree_for_auth_org(
        factory, "No Structure Document"
    )
    pdf = _make_plain_pdf(["Plain unstructured memo content without any headings."])
    fake_storage.objects[version.storage_key] = pdf
    assert await _run_chain(factory, org_id, version.id) == "COMPLETED"

    response = await app_client.get(f"/documents/{doc.id}/toc", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_structure"] is False
    assert payload["section_count"] == 0
    assert payload["items"] == []


@pytest.mark.integration
async def test_chunks_api_returns_provenance(app_client, worker_env, fake_storage):
    factory = worker_env

    headers = await _auth_headers(app_client)

    org_id, doc, version = await _tree_for_auth_org(factory, "Chunks API Document")
    pdf = _make_structured_pdf(STRUCTURED_PAGES + TABLE_PAGE)
    fake_storage.objects[version.storage_key] = pdf
    assert await _run_chain(factory, org_id, version.id) == "COMPLETED"

    response = await app_client.get(
        f"/documents/{doc.id}/chunks", headers=headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["document_id"] == doc.id
    assert payload["status"] == "CHUNKING"
    assert payload["total"] == len(payload["items"]) > 0

    first = payload["items"][0]
    assert first["chunk_index"] == 0
    assert first["content"]
    assert first["token_count"] > 0
    assert len(first["content_hash"]) == 64
    assert first["has_embedding"] is False  # Phase 7 fills embeddings
    assert first["start_page"] is not None and first["end_page"] is not None

    # Provenance fields present for a nested section
    nested = [
        item for item in payload["items"]
        if item["section_number"] == "4.1"
    ]
    assert nested
    assert nested[0]["heading_path"] == ["Approval Process", "Marketing Review"]
    assert nested[0]["section_id"]

    # Table rows are whole
    table_items = [i for i in payload["items"] if i["contains_table"]]
    assert table_items
    for item in table_items:
        for line in item["content"].split("\n"):
            if "|" in line:
                assert len(line.split("|")) == 4

    # Pagination works
    response = await app_client.get(
        f"/documents/{doc.id}/chunks",
        params={"offset": 0, "limit": 1},
        headers=headers,
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1

    # Unauthenticated access is rejected
    response = await app_client.get(f"/documents/{doc.id}/chunks")
    assert response.status_code == 401
