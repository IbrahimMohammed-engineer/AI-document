"""
Integration tests for the Phase 5 extraction/OCR pipeline.

Runs against testcontainers PostgreSQL (full Alembic stack incl.
document_pages) + Redis, mirroring the Phase 4 harness:

  - born-digital PDF â†’ pages with ocr_used=false + page_count backfill
  - mixed PDF (text page + scanned page) â†’ per-page OCR routing
  - scanned PDF with OCR_PROVIDER=none â†’ explicit per-page "OCR failed"
    markers; the document still COMPLETES (partial processing, Backend Â§19)
  - corrupt PDF â†’ deterministic terminal FAILED (no retry budget spent)
  - content that defeats magic-byte sniffing â†’ FILE_TYPE_UNSUPPORTED
  - declared-mime mismatch â†’ parsed by DETECTED type (defense-in-depth)
  - pre-persisted pages â†’ resume without duplicates (idempotency, Â§49)
  - simulated worker crash mid-extraction â†’ RETRYING, then resume completes
    with exactly the full page set
  - GET /documents/{id}/pages returns extracted pages via the API

The Arq worker loop is not started â€” run_processing_job is invoked directly
against real PostgreSQL/Redis (exactly what the worker executes per pointer).

Requires Docker (testcontainers). Tesseract is NOT required â€” OCR behavior
is exercised through fake providers and the NullOCRProvider degradation path.
"""
from __future__ import annotations

import io
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
import app.workers.jobs as jobs_mod
from app.infrastructure.ocr import (
    OCRResult,
    PageContext,
)
from app.infrastructure.storage import (
    ObjectStorageProvider,
    StorageObjectMissingError,
)
from app.models.document import Document, DocumentVersion
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_page_repository import DocumentPageRepository
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.repositories.user_repository import OrganizationRepository, UserRepository
from app.workers.jobs import reconciliation_sweep  # noqa: F401 (harness parity)
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
    "comparison_changes",
    "document_comparisons",
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


# â”€â”€â”€ Fixtures (harness parity with Phase 4) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
    """In-memory ObjectStorageProvider â€” mirrors the real interface."""

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
    """Deterministic OCRProvider stand-in (cloud engines are stubbed per the
    roadmap; Tesseract's real contract is covered by the unit suite)."""

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
    """Route the worker's OCR provider resolution to the fake."""
    provider = FakeOCRProvider()
    monkeypatch.setattr(jobs_mod, "get_ocr_provider", lambda: provider)
    return provider


# â”€â”€â”€ Document fixtures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _make_pdf(page_specs: list[dict]) -> bytes:
    """Build a PDF; spec: {"text": str} for a native page, {"image": bool}
    for an image-only (scanned) page."""
    import fitz
    from PIL import Image

    doc = fitz.open()
    for spec in page_specs:
        page = doc.new_page()
        if "text" in spec:
            page.insert_text((72, 72), spec["text"], fontsize=12)
        if spec.get("image"):
            buf = io.BytesIO()
            Image.new("RGB", (400, 300), color="#dddddd").save(buf, format="PNG")
            page.insert_image(fitz.Rect(100, 100, 500, 400), stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


async def _make_document_tree(
    factory: async_sessionmaker,
    *,
    slug: str = "extract-org",
    mime_type: str = "application/pdf",
) -> tuple[Organization, User, Document, DocumentVersion]:
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name="Extract Test Org", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Extract Admin",
            password_hash="$argon2id$test",
        )
        doc = await DocumentRepository(session).create(
            organization_id=org.id,
            owner_id=user.id,
            name="Phase 5 Extraction Document",
            document_type="policy",
        )
        version = await DocumentVersionRepository(session).create(
            document_id=doc.id,
            version_number=1,
            storage_key=f"organizations/{org.id}/documents/{doc.id}/versions/v1/original.pdf",
            mime_type=mime_type,
            file_size_bytes=0,  # authoritative size recorded by the handler
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


# â”€â”€â”€ Born-digital PDF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_born_digital_pdf_extracts_pages(worker_env, fake_storage):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_pdf([
        {"text": "Born digital alpha page with plenty of readable content."},
        {"text": "Born digital beta page with plenty of readable content."},
        {"text": "Born digital gamma page with plenty of readable content."},
    ])
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    job = await _get_job(factory, job_id)
    assert job.status == "COMPLETED"
    assert job.progress == 100

    version = await _get_version(factory, version.id)
    # EXTRACTION completed; the chained CHUNKING job stays PENDING here â€”
    # running it to COMPLETED (and the version to CHUNKING) is covered by
    # test_chunking_pipeline.py (Phase 6).
    assert version.status == "EXTRACTING"
    assert version.page_count == 3

    pages = await _get_pages(factory, version.id)
    assert sorted(p.page_number for p in pages) == [1, 2, 3]
    assert all(p.ocr_used is False for p in pages)
    assert all("readable content" in p.text for p in pages)
    assert all(p.width is not None and p.height is not None for p in pages)
    by_number = {p.page_number: p for p in pages}
    assert "alpha" in by_number[1].text
    assert "gamma" in by_number[3].text
    # Native pages carry no OCR provenance metadata
    assert by_number[1].page_metadata in (None, {})


# â”€â”€â”€ Mixed PDF: per-page OCR routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_mixed_pdf_routes_ocr_per_page(worker_env, fake_storage, fake_ocr):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_pdf([
        {"text": "Native first page with enough alphanumeric density here."},
        {"image": True},  # scanned exhibit â€” no native text
        {"text": "Native third page with enough alphanumeric density too."},
    ])
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    # Only the scanned page went through OCR â€” per-page, not per-document
    assert sorted(fake_ocr.calls) == [2]

    pages = {p.page_number: p for p in await _get_pages(factory, version.id)}
    assert pages[1].ocr_used is False
    assert pages[2].ocr_used is True
    assert pages[2].text == "FAKE OCR TEXT FROM SCAN"
    assert pages[3].ocr_used is False

    # OCR provenance recorded in page metadata (internal-only quality signal)
    ocr_meta = pages[2].page_metadata["ocr"]
    assert ocr_meta["provider"] == "fake-ocr"
    assert ocr_meta["confidence"] == 90.0
    assert pages[1].page_metadata in (None, {})

    version = await _get_version(factory, version.id)
    assert version.status == "OCR"  # the OCR sub-stage was entered
    assert version.page_count == 3


# â”€â”€â”€ Scanned PDF without any OCR provider: partial processing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_scanned_pdf_without_provider_completes_with_markers(
    worker_env, fake_storage
):
    """OCR_PROVIDER defaults to none â†’ NullOCRProvider â†’ each scanned page
    gets the explicit OCR-failed marker and the document still completes."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_pdf([{"image": True}, {"image": True}])
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    pages = await _get_pages(factory, version.id)
    assert len(pages) == 2
    for page in pages:
        assert page.ocr_used is True
        assert page.text == ""           # explicit empty text
        assert page.page_metadata["ocr_failed"] is True
        assert "OCR_PROVIDER" in page.page_metadata["ocr_error"]

    version = await _get_version(factory, version.id)
    assert version.status == "OCR"
    assert version.page_count == 2
    assert version.error_message is None  # NOT a version failure


# â”€â”€â”€ Deterministic failures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_corrupt_pdf_fails_terminally(worker_env, fake_storage):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    # Magic bytes say PDF; the body is garbage
    fake_storage.objects[version.storage_key] = (
        b"%PDF-1.4 this is not actually a valid pdf body at all"
    )

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "FAILED"

    job = await _get_job(factory, job_id)
    assert job.status == "FAILED"
    assert job.attempts == 1  # deterministic â€” no retry budget spent

    version = await _get_version(factory, version.id)
    assert version.status == "FAILED"
    assert version.error_message is not None

    records = await queue_mod.read_dead_letter()
    assert len(records) == 1
    assert records[0]["job_id"] == job_id

    # No pages were persisted
    assert await _get_pages(factory, version.id) == []


@pytest.mark.integration
async def test_unidentifiable_content_fails_as_unsupported(worker_env, fake_storage):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    fake_storage.objects[version.storage_key] = (
        b"plain text masquerading as a document upload"
    )

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "FAILED"

    job = await _get_job(factory, job_id)
    assert job.status == "FAILED"
    # Deterministic terminal failure — no retry budget spent
    assert job.attempts == 1
    assert "could not be identified" in (job.error_message or "")

    version = await _get_version(factory, version.id)
    assert version.status == "FAILED"


@pytest.mark.integration
async def test_declared_mime_mismatch_parses_by_detected_type(
    worker_env, fake_storage
):
    """Upload-time declared type is advisory â€” the worker trusts magic bytes."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(
        factory,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    pdf = _make_pdf([{"text": "Actually a pdf despite the declared docx mime."}])
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    pages = await _get_pages(factory, version.id)
    assert len(pages) == 1
    assert "declared docx mime" in pages[0].text


# â”€â”€â”€ Idempotent resume â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_resume_skips_already_persisted_pages(worker_env, fake_storage):
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    pdf = _make_pdf([
        {"text": f"Resume test page {n} with plenty of readable content."}
        for n in range(1, 6)
    ])
    fake_storage.objects[version.storage_key] = pdf

    # Simulate a prior partial run: pages 1-2 already persisted
    async with factory() as session:
        repo = DocumentPageRepository(session)
        await repo.insert_pages([
            {
                "document_version_id": version.id,
                "page_number": 1,
                "text": "Resume test page 1 with plenty of readable content.",
                "ocr_used": False,
                "width": 612.0,
                "height": 792.0,
                "metadata": None,
            },
            {
                "document_version_id": version.id,
                "page_number": 2,
                "text": "Resume test page 2 with plenty of readable content.",
                "ocr_used": False,
                "width": 612.0,
                "height": 792.0,
                "metadata": None,
            },
        ])
        await session.commit()

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    pages = sorted(await _get_pages(factory, version.id),
                   key=lambda p: p.page_number)
    # Exactly 5 pages â€” the pre-persisted ones were skipped, not duplicated
    assert [p.page_number for p in pages] == [1, 2, 3, 4, 5]
    assert all("Resume test page" in p.text for p in pages)

    version = await _get_version(factory, version.id)
    assert version.page_count == 5


@pytest.mark.integration
async def test_worker_crash_mid_extraction_resumes_without_duplicates(
    worker_env, fake_storage, fake_ocr, monkeypatch
):
    """Commit-crash-resume: batched pages survive the crash; the retry
    continues from the last persisted page and never duplicates."""
    factory = worker_env
    monkeypatch.setenv("EXTRACTION_PAGE_BATCH_SIZE", "2")
    from app.core.config import get_settings

    get_settings.cache_clear()

    org, user, doc, version = await _make_document_tree(factory)
    pdf = _make_pdf([{"image": True} for _ in range(5)])  # scanned pages â†’ OCR path
    fake_storage.objects[version.storage_key] = pdf

    # Transient crash while processing page 3 â€” AFTER the first batch
    # (pages 1-2) has committed, simulating a worker killed mid-job.
    import app.ingestion.extractor as extractor_mod

    original_recognize = extractor_mod._recognize_page
    state = {"crashed": False}

    async def crashing_recognize(page, **kwargs):
        if page.page_number == 3 and not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated worker crash mid-extraction")
        return await original_recognize(page, **kwargs)

    monkeypatch.setattr(extractor_mod, "_recognize_page", crashing_recognize)

    job_id = await _make_job(factory, organization_id=org.id,
                             document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "RETRYING"

    # Pages 1-2 are durable despite the crash
    pages = await _get_pages(factory, version.id)
    assert sorted(p.page_number for p in pages) == [1, 2]

    # Second execution (the retry pointer fires) â€” resumes from page 3
    assert await run_processing_job({}, job_id) == "COMPLETED"

    pages = sorted(await _get_pages(factory, version.id),
                   key=lambda p: p.page_number)
    assert [p.page_number for p in pages] == [1, 2, 3, 4, 5]  # no duplicates
    assert all(p.text == "FAKE OCR TEXT FROM SCAN" for p in pages)

    job = await _get_job(factory, job_id)
    assert job.status == "COMPLETED"

    version = await _get_version(factory, version.id)
    assert version.status == "OCR"
    assert version.page_count == 5
    get_settings.cache_clear()


# â”€â”€â”€ Pages API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

REGISTER_BODY = {
    "org_name": "Pages Corp",
    "slug": "pages-corp",
    "email": "admin@pages-corp.com",
    "full_name": "Pages Admin",
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


@pytest.mark.integration
async def test_pages_endpoint_returns_extracted_pages(
    app_client, worker_env, fake_storage, fake_ocr
):
    # app_client declared FIRST so its identity-table teardown runs LAST
    # (worker_env tears down first, clearing FK-referencing document rows).
    factory = worker_env

    # Register/login first — the auth org must exist before the row lookup
    headers = await _auth_headers(app_client)

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
    assert row is not None
    org_id, user_id = str(row[0]), str(row[1])

    async with factory() as session:
        doc = await DocumentRepository(session).create(
            organization_id=org_id,
            owner_id=user_id,
            name="Pages API Document",
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

    pdf = _make_pdf([
        {"text": "API page one with plenty of readable content."},
        {"image": True},
    ])
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(
        factory, organization_id=org_id, document_version_id=version.id
    )
    assert await run_processing_job({}, job_id) == "COMPLETED"

    response = await app_client.get(
        f"/documents/{doc.id}/pages", headers=headers
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["document_id"] == doc.id
    assert payload["version_id"] == version.id
    assert payload["page_count"] == 2
    assert payload["total"] == 2
    assert [item["page_number"] for item in payload["items"]] == [1, 2]
    assert payload["items"][0]["ocr_used"] is False
    assert payload["items"][1]["ocr_used"] is True
    assert payload["items"][1]["ocr_failed"] is False
    assert "API page one" in payload["items"][0]["text"]

    # Version filter + pagination parameters work
    response = await app_client.get(
        f"/documents/{doc.id}/pages",
        params={"version": 1, "offset": 1, "limit": 1},
        headers=headers,
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["page_number"] == 2

    # Unauthenticated access is rejected
    response = await app_client.get(f"/documents/{doc.id}/pages")
    assert response.status_code == 401
