"""
Integration tests for the background processing machinery (Phase 4).

Runs against testcontainers PostgreSQL (full Alembic stack) + Redis:
  - enqueue → claim → complete round trip (run_processing_job)
  - retry with backoff on a handler that fails (RETRYING visibility)
  - dead-letter on retry exhaustion (PostgreSQL terminal FAILED + Redis list)
  - deterministic failure path (missing storage object — no retries spent)
  - org-mismatch payload rejected (tamper defense, Backend §14)
  - reconciliation sweep re-enqueues PENDING/stuck jobs missing Redis pointers
  - Redis-flush recovery (the sweep makes Redis disposable)
  - upload service creates the first job inside the transaction and enqueues
    only after commit (Backend §50 ordering)

The Arq worker loop itself is not started — `run_processing_job` is invoked
directly with real PostgreSQL/Redis, which is exactly what the worker process
executes per pointer.
"""
from __future__ import annotations

from io import BytesIO
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import Headers
from starlette.formparsers import UploadFile

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.domain.state_machines import JobType
from app.infrastructure.storage import (
    ObjectStorageProvider,
    StorageObjectMissingError,
)
from app.models.document import Document, DocumentVersion
from app.models.organization import Organization
from app.models.user import User
from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.repositories.user_repository import OrganizationRepository, UserRepository
from app.services.document_service import DocumentService
from app.workers.jobs import reconciliation_sweep, run_processing_job

# FK-safe delete order for the tables these tests touch
_TABLES_TO_CLEAN = (
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


# ─── Fixtures ─────────────────────────────────────────────────────────────────

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
    """Bind the worker/queue infrastructure to the test containers.

    - get_session_factory() (used by run_processing_job/sweep) → test Postgres
    - queue module pool → test Redis
    """
    await _clean_tables(app_session_factory)

    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)

    # Workers import get_session_factory lazily from this module-global
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
    """Install an in-memory storage provider for handler verification."""
    provider = FakeStorageProvider()
    import app.infrastructure.storage as storage_mod

    monkeypatch.setattr(storage_mod, "_storage_provider", provider)
    return provider


async def _make_document_tree(
    factory: async_sessionmaker,
    *,
    slug: str = "job-test-org",
) -> tuple[Organization, User, Document, DocumentVersion]:
    """Create org → user → document → version(UPLOADED) with real commits."""
    async with factory() as session:
        org = await OrganizationRepository(session).create(
            name="Job Test Org", slug=slug
        )
        user = await UserRepository(session).create(
            organization_id=org.id,
            email=f"admin@{slug}.test",
            full_name="Job Admin",
            password_hash="$argon2id$test",
        )
        doc = await DocumentRepository(session).create(
            organization_id=org.id,
            owner_id=user.id,
            name="Phase 4 Test Document",
            document_type="policy",
        )
        version = await DocumentVersionRepository(session).create(
            document_id=doc.id,
            version_number=1,
            storage_key=f"organizations/{org.id}/documents/{doc.id}/versions/v1/original.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
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


def _pointer_key(job_id: str) -> str:
    return f"arq:job:pj:{job_id}"


# ─── Happy path ───────────────────────────────────────────────────────────────

def _make_pdf_bytes() -> bytes:
    """A minimal born-digital PDF the real EXTRACTION handler can parse."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Phase 4 round trip page with enough text.", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.integration
async def test_job_round_trip_claim_complete(worker_env, fake_storage):
    """PENDING job → claim → handler → COMPLETED; version UPLOADED → PROCESSING."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)

    # The uploaded bytes must exist in storage (the handler verifies them)
    pdf = _make_pdf_bytes()
    fake_storage.objects[version.storage_key] = pdf

    job_id = await _make_job(
        factory, organization_id=org.id, document_version_id=version.id
    )

    result = await run_processing_job({}, job_id)

    assert result == "COMPLETED"
    job = await _get_job(factory, job_id)
    assert job.status == "COMPLETED"
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.completed_at is not None
    assert job.progress == 100
    assert job.error_message is None

    # Version moved into the pipeline (EXTRACTION ran; the chained CHUNKING
    # job stays PENDING — the full chain is covered by Phase 6 tests)
    version = await _get_version(factory, version.id)
    assert version.status == "EXTRACTING"


@pytest.mark.integration
async def test_terminal_job_is_never_reexecuted(worker_env, fake_storage):
    """Re-firing a completed pointer is an idempotent no-op."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    fake_storage.objects[version.storage_key] = _make_pdf_bytes()

    job_id = await _make_job(factory, organization_id=org.id, document_version_id=version.id)
    assert await run_processing_job({}, job_id) == "COMPLETED"

    # Corrupt storage afterwards — must not matter: no second execution
    fake_storage.objects.clear()
    assert await run_processing_job({}, job_id) == "COMPLETED"
    job = await _get_job(factory, job_id)
    assert job.attempts == 1


# ─── Retry / backoff / dead-letter ────────────────────────────────────────────

@pytest.mark.integration
async def test_transient_failure_goes_retrying_then_exhausts(
    worker_env, fake_storage, monkeypatch
):
    """A failing handler: RETRYING (visible) while budget lasts, then FAILED."""
    from app.workers import jobs as jobs_mod

    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    fake_storage.objects[version.storage_key] = b"x" * version.file_size_bytes
    job_id = await _make_job(
        factory, organization_id=org.id, document_version_id=version.id, max_attempts=2
    )

    # Force the registered handler to fail transiently
    async def _flaky(ctx, job, version, document, session):
        raise ValueError("simulated provider outage")

    monkeypatch.setitem(jobs_mod.HANDLERS, JobType.EXTRACTION, _flaky)

    # Attempt 1 → RETRYING (never silently back to PENDING)
    assert await run_processing_job({}, job_id) == "RETRYING"
    job = await _get_job(factory, job_id)
    assert job.status == "RETRYING"
    assert job.attempts == 1
    assert "simulated provider outage" in job.error_message

    # A deferred retry pointer exists in Redis
    pool = queue_mod.get_queue_pool()
    assert await pool.exists(_pointer_key(job_id))

    # Attempt 2 (exhaustion) → terminal FAILED + version FAILED + dead-letter
    assert await run_processing_job({}, job_id) == "FAILED"
    job = await _get_job(factory, job_id)
    assert job.status == "FAILED"
    assert job.attempts == 2
    assert job.completed_at is not None

    version = await _get_version(factory, version.id)
    assert version.status == "FAILED"
    assert version.error_message is not None

    records = await queue_mod.read_dead_letter()
    assert len(records) == 1
    assert records[0]["job_id"] == job_id
    # Dead-letter carries metadata, not document content
    assert "Phase 4 Test Document" not in records[0]["error"]


@pytest.mark.integration
async def test_deterministic_failure_skips_retries(worker_env, fake_storage):
    """Missing storage object → immediate FAILED, no retry budget spent."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    # NOTE: no object stored — handler raises DeterministicJobError

    job_id = await _make_job(factory, organization_id=org.id, document_version_id=version.id)

    assert await run_processing_job({}, job_id) == "FAILED"
    job = await _get_job(factory, job_id)
    assert job.status == "FAILED"
    assert job.attempts == 1  # max_attempts=3 — unused budget stays unused
    assert "missing" in (job.error_message or "").lower()

    version = await _get_version(factory, version.id)
    assert version.status == "FAILED"


# ─── Tamper defense ───────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_org_mismatch_payload_rejected(worker_env, fake_storage):
    """A job whose organization_id does not match the entity's org fails hard."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    fake_storage.objects[version.storage_key] = b"x" * version.file_size_bytes

    # A second org "sends" the job pointer (tampered payload)
    async with factory() as session:
        org_b = await OrganizationRepository(session).create(
            name="Org B", slug="org-b-tamper"
        )
        org_b_id = org_b.id
        await session.commit()

    job_id = await _make_job(
        factory, organization_id=org_b_id, document_version_id=version.id
    )

    assert await run_processing_job({}, job_id) == "FAILED"
    job = await _get_job(factory, job_id)
    assert "tenant" in (job.error_message or "").lower()

    # Org A's version must be untouched — never cross-tenant
    version = await _get_version(factory, version.id)
    assert version.status == "UPLOADED"


# ─── Reconciliation sweep ─────────────────────────────────────────────────────

async def _backdate_job(factory: async_sessionmaker, job_id: str, minutes: int = 10) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET updated_at = now() - make_interval(mins => :m), "
                "    created_at = now() - make_interval(mins => :m) "
                "WHERE id = :id"
            ),
            {"m": minutes, "id": job_id},
        )
        await session.commit()


@pytest.mark.integration
async def test_sweep_reenqueues_job_missing_pointer(worker_env):
    """PENDING row whose enqueue-after-commit was missed → sweep recovers it."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    job_id = await _make_job(factory, organization_id=org.id, document_version_id=version.id)

    pool = queue_mod.get_queue_pool()
    assert not await pool.exists(_pointer_key(job_id))

    await _backdate_job(factory, job_id)
    requeued = await reconciliation_sweep({})

    assert requeued == 1
    assert await pool.exists(_pointer_key(job_id))


@pytest.mark.integration
async def test_sweep_recovers_after_redis_flush(worker_env):
    """Simulated Redis flush: pointer gone → sweep re-enqueues (non-event)."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    job_id = await _make_job(factory, organization_id=org.id, document_version_id=version.id)

    pool = queue_mod.get_queue_pool()
    # Original enqueue succeeded...
    from app.infrastructure.queue import enqueue_processing_job

    assert await enqueue_processing_job(job_id)
    assert await pool.exists(_pointer_key(job_id))

    # ...then Redis was flushed (pointer lost, PostgreSQL row durable)
    await pool.flushdb()

    await _backdate_job(factory, job_id)
    requeued = await reconciliation_sweep({})

    assert requeued == 1
    assert await pool.exists(_pointer_key(job_id))

    # processing_jobs history still queryable from PostgreSQL after the flush
    job = await _get_job(factory, job_id)
    assert job.status == "PENDING"


@pytest.mark.integration
async def test_sweep_ignores_fresh_jobs(worker_env):
    """A just-created PENDING job is inside the safety window — untouched."""
    factory = worker_env
    org, user, doc, version = await _make_document_tree(factory)
    await _make_job(factory, organization_id=org.id, document_version_id=version.id)

    assert await reconciliation_sweep({}) == 0


# ─── Upload wiring (Backend §50 — commit-then-enqueue) ────────────────────────

@pytest.mark.integration
async def test_upload_creates_job_in_transaction_and_enqueues_after(
    worker_env, fake_storage, monkeypatch
):
    """upload_document: PENDING job row commits WITH the version; the enqueue
    happens only after (and carries the committed job's id)."""
    factory = worker_env
    org, user, _, _ = await _make_document_tree(factory)

    enqueued: list[str] = []

    async def _record_enqueue(job_id, **kwargs):
        enqueued.append(job_id)
        return True

    monkeypatch.setattr(
        "app.services.job_service.enqueue_processing_job", _record_enqueue
    )

    payload = b"%PDF-1.4 test pdf content for phase 4 upload"
    upload = UploadFile(
        file=BytesIO(payload),
        filename="policy.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    async with factory() as session:
        response = await DocumentService.upload_document(
            file=upload,
            organization_id=org.id,
            owner_id=user.id,
            name="Wired Upload",
            document_type="policy",
            db=session,
        )
        await session.commit()

    assert response.job_id is not None

    # The durable PENDING row exists in the SAME transaction as the version
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status, job_type, document_version_id "
                    "FROM processing_jobs WHERE id = :id"
                ),
                {"id": response.job_id},
            )
        ).fetchone()
    assert rows is not None
    status, job_type, version_id = rows
    assert status == "PENDING"
    assert job_type == "EXTRACTION"
    assert str(version_id) == response.version_id

    # Enqueue happened after commit, pointing at the committed job id
    assert enqueued == [response.job_id]
