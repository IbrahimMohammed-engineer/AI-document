"""
API integration tests for the Phase 4 processing endpoints.

Runs against the real FastAPI app (httpx ASGI) + testcontainers PostgreSQL/Redis:

  - GET  /documents/processing   â†’ org-wide active jobs (header widget)
  - GET  /documents/{id}/status  â†’ polling snapshot shape
  - POST /documents/{id}/retry   â†’ 409 without a failed job; 202 + state
                                   transition FAILED â†’ PROCESSING with one
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.repositories.document_repository import (
    DocumentRepository,
    DocumentVersionRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)

# â”€â”€â”€ Constants / helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

REGISTER_BODY = {
    "org_name": "Processing Corp",
    "slug": "processing-corp",
    "email": "admin@processing-corp.com",
    "full_name": "Pipeline Admin",
    "password": "super-secret-1",
}

_DOCUMENT_TABLES = (
    "processing_jobs",
    "collection_documents",
    "document_tags",
    "collections",
    "document_versions",
    "documents",
)


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
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
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _org_and_user_ids(factory: async_sessionmaker) -> tuple[str, str]:
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
    return str(row[0]), str(row[1])


async def _seed_document_with_job(
    factory: async_sessionmaker,
    *,
    org_id: str,
    user_id: str,
    job_status: str = "PENDING",
    version_status: str = "UPLOADED",
    job_type: str = "EXTRACTION",
    fail_version: bool = False,
) -> tuple[str, str, str]:
    """Create document â†’ version â†’ job with real commits; returns (doc, version, job) ids."""
    async with factory() as session:
        doc = await DocumentRepository(session).create(
            organization_id=org_id,
            owner_id=user_id,
            name="Processing API Doc",
            document_type="policy",
        )
        version = await DocumentVersionRepository(session).create(
            document_id=doc.id,
            version_number=1,
            storage_key=f"organizations/{org_id}/documents/{doc.id}/versions/v1/original.pdf",
            mime_type="application/pdf",
            file_size_bytes=2048,
            created_by=user_id,
            status=version_status,
        )
        if fail_version:
            await session.execute(
                text(
                    "UPDATE document_versions SET error_message = :err WHERE id = :id"
                ),
                {"err": "Retries exhausted. Last error: boom", "id": version.id},
            )
        doc.current_version_id = version.id
        await session.flush()

        job = await ProcessingJobRepository(session).create(
            organization_id=org_id,
            document_version_id=version.id,
            job_type=job_type,
        )
        if job_status == "FAILED":
            await ProcessingJobRepository(session).mark_failed(
                job, error_message="boom", now=_utc_now()
            )
        elif job_status == "PROCESSING":
            await ProcessingJobRepository(session).mark_processing(job, now=_utc_now())
        await session.commit()
        return doc.id, version.id, job.id


async def _clean_document_tables(factory: async_sessionmaker) -> None:
    async with factory() as session:
        for table in _DOCUMENT_TABLES:
            await session.execute(text(f'DELETE FROM "{table}"'))
        await session.commit()


# â”€â”€â”€ Fixtures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest_asyncio.fixture()
async def processing_env(
    app_client: AsyncClient,
    app_session_factory: async_sessionmaker,
    monkeypatch,
) -> AsyncGenerator[async_sessionmaker, None]:
    """Authenticated API context with clean document tables per test."""
    await _clean_document_tables(app_session_factory)

    enqueued: list[str] = []

    async def _record_enqueue(job_id, **kwargs):
        enqueued.append(job_id)
        return True

    monkeypatch.setattr(
        "app.services.job_service.enqueue_processing_job", _record_enqueue
    )
    # Expose the recorder to tests through the factory object attribute
    app_session_factory._enqueued = enqueued  # type: ignore[attr-defined]

    yield app_session_factory

    await _clean_document_tables(app_session_factory)


# â”€â”€â”€ GET /documents/processing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_processing_list_returns_active_jobs(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    factory = processing_env
    headers = await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, version_id, job_id = await _seed_document_with_job(
        factory, org_id=org_id, user_id=user_id
    )

    response = await app_client.get("/documents/processing", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["job_id"] == job_id
    assert item["document_id"] == doc_id
    assert item["document_name"] == "Processing API Doc"
    assert item["job_type"] == "EXTRACTION"
    assert item["status"] == "PENDING"
    assert item["version_number"] == 1
    assert item["attempts"] == 0


# â”€â”€â”€ GET /documents/{id}/status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_status_endpoint_shape(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    factory = processing_env
    headers = await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, version_id, job_id = await _seed_document_with_job(
        factory, org_id=org_id, user_id=user_id
    )

    response = await app_client.get(f"/documents/{doc_id}/status", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == doc_id
    assert body["version_id"] == version_id
    assert body["status"] == "UPLOADED"          # version status
    assert body["current_step"] == "EXTRACTION"  # live job detail
    assert body["job_id"] == job_id
    assert body["job_status"] == "PENDING"
    assert body["error_message"] is None


@pytest.mark.integration
async def test_status_endpoint_requires_auth(app_client: AsyncClient):
    response = await app_client.get(
        "/documents/00000000-0000-0000-0000-000000000000/status"
    )
    assert response.status_code == 401


# â”€â”€â”€ POST /documents/{id}/retry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
async def test_retry_without_failed_job_conflicts(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    factory = processing_env
    headers = await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, _, _ = await _seed_document_with_job(
        factory, org_id=org_id, user_id=user_id, job_status="PENDING"
    )

    response = await app_client.post(f"/documents/{doc_id}/retry", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"
    assert processing_env._enqueued == []  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_retry_transitions_failed_to_processing(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    factory = processing_env
    headers = await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, version_id, failed_job_id = await _seed_document_with_job(
        factory,
        org_id=org_id,
        user_id=user_id,
        job_status="FAILED",
        version_status="FAILED",
        fail_version=True,
    )

    response = await app_client.post(f"/documents/{doc_id}/retry", headers=headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["job_type"] == "EXTRACTION"
    assert body["version_status"] == "PROCESSING"
    assert body["job_id"] != failed_job_id

    # A fresh job row exists; the version transitioned explicitly
    async with factory() as session:
        new_job_status = (
            await session.execute(
                text("SELECT status FROM processing_jobs WHERE id = :id"),
                {"id": body["job_id"]},
            )
        ).scalar_one()
        version_status = (
            await session.execute(
                text("SELECT status FROM document_versions WHERE id = :id"),
                {"id": version_id},
            )
        ).scalar_one()
    assert new_job_status == "PENDING"
    assert version_status == "PROCESSING"

    # Exactly one enqueue, after commit, for the new job
    assert processing_env._enqueued == [body["job_id"]]  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_retry_conflicts_while_processing_in_flight(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    factory = processing_env
    headers = await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, _, _ = await _seed_document_with_job(
        factory, org_id=org_id, user_id=user_id, job_status="PROCESSING"
    )

    response = await app_client.post(f"/documents/{doc_id}/retry", headers=headers)

    assert response.status_code == 409, response.text
    assert "already in progress" in response.json()["error"]["message"]


@pytest.mark.integration
async def test_status_endpoint_isolated_per_org(
    app_client: AsyncClient, processing_env: async_sessionmaker
):
    """A second org's admin cannot see (404) the first org's document status."""
    from httpx import ASGITransport

    from app.main import app as fastapi_app

    factory = processing_env
    # Register/login org A first — _org_and_user_ids looks up its rows
    await _auth_headers(app_client)
    org_id, user_id = await _org_and_user_ids(factory)
    doc_id, _, _ = await _seed_document_with_job(
        factory, org_id=org_id, user_id=user_id
    )

    # Register a second org + login from an independent cookie jar
    other = AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test")
    try:
        reg = await other.post(
            "/auth/register",
            json={
                "org_name": "Other Org",
                "slug": "other-org",
                "email": "admin@other.com",
                "full_name": "Other Admin",
                "password": "super-secret-1",
            },
        )
        assert reg.status_code == 201
        login = await other.post(
            "/auth/login",
            json={
                "email": "admin@other.com",
                "password": "super-secret-1",
                "org_slug": "other-org",
            },
        )
        other_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        response = await app_client.get(
            f"/documents/{doc_id}/status", headers=other_headers
        )
        assert response.status_code == 404
    finally:
        await other.aclose()
