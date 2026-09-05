"""
Integration tests — the structured-extraction pipeline (Phase 14, plan §8.3).

Named ``test_structured_extraction_pipeline`` — deliberately distinct from
``test_extraction_pipeline.py`` (the Phase 5 ingestion text-extraction
stage), mirroring the JobType.EXTRACTION vs JobType.STRUCTURED_EXTRACTION
name-collision correction (plan §2.4).

Same harness as test_summary_pipeline.py: services invoked directly (no Arq
loop).  Covers the roadmap exit criterion 4: an extraction run over a
fixture contract yields cited dates/parties/requirements — plus the
STRUCTURED_EXTRACTION dispatch-trap regression (mirrors the summary one)
and the chat reuse-latest-completed rule (§2.6 point 5).

Requires Docker (testcontainers).
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
from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.llm import set_llm_provider
from app.repositories.document_extraction_repository import (
    DocumentExtractionRepository,
)
from app.services.extraction_service import ExtractionService
from app.services.job_service import JobService
from app.workers.jobs import run_processing_job
from tests.fixtures.extraction_fixtures import (
    install_extraction_llm_stub,
    seed_contract_document,
    seed_extraction_org,
)

_TABLES_TO_CLEAN = (
    "message_feedback",
    "citations",
    "messages",
    "conversation_documents",
    "conversations",
    "conflict_statements",
    "conflicts",
    "comparison_changes",
    "document_comparisons",
    "document_extraction_items",
    "document_extractions",
    "document_summaries",
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


async def _clean_tables(factory: async_sessionmaker) -> None:
    async with factory() as session:
        for table in _TABLES_TO_CLEAN:
            await session.execute(text(f'DELETE FROM "{table}"'))
        await session.commit()


@pytest_asyncio.fixture()
async def extraction_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> AsyncGenerator[async_sessionmaker, None]:
    """Bind worker/queue infrastructure + stubbed AI providers."""
    await _clean_tables(app_session_factory)

    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    install_extraction_llm_stub()
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))

    yield app_session_factory

    await _clean_tables(app_session_factory)
    await pool.aclose()
    set_llm_provider(None)
    set_embedding_provider(None)


async def _run_job_now(factory: async_sessionmaker, job_id: str) -> str:
    from app.repositories.processing_job_repository import ProcessingJobRepository

    async with factory() as session:
        job = await ProcessingJobRepository(session).get_by_id(job_id)
        assert job is not None
        job.status = "PENDING"
        await session.commit()
    return await run_processing_job({}, job_id)


@pytest.mark.integration
class TestStructuredExtractionPipeline:

    async def test_contract_yields_cited_items(
        self, extraction_env: async_sessionmaker
    ):
        """Exit criterion 4: cited date/party/requirement items from a
        fixture contract — each item's provenance resolves to a real chunk."""
        factory = extraction_env
        org, user = await seed_extraction_org(factory)
        doc = await seed_contract_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            extraction = await ExtractionService.create_run(
                user=user,
                document_version_id=doc["version_id"],
                schema_key="standard_v1",
                db=session,
            )
            job = await JobService.create_for_extraction(
                session,
                organization_id=org.id,
                document_version_id=doc["version_id"],
                extraction_id=extraction.id,
            )
            await session.commit()

        status = await _run_job_now(factory, job.id)
        assert status == "COMPLETED"

        async with factory() as session:
            repo = DocumentExtractionRepository(session)
            done = await repo.get_by_id(extraction.id)
            assert done is not None and done.status == "COMPLETED"
            items = await repo.list_items(extraction.id)
            assert items, "extraction must yield items for the fixture contract"

            categories = {item.category for item in items}
            assert {"requirement", "party"} <= categories

            real_chunks = {
                row[0]
                for row in (
                    await session.execute(text("SELECT id FROM document_chunks"))
                ).fetchall()
            }
            for item in items:
                assert item.chunk_id in real_chunks
                assert item.quoted_text
                assert item.page_number >= 1

    async def test_ready_version_dispatch_trap_regression(
        self, extraction_env: async_sessionmaker
    ):
        """§2.3/§8.3 mirrored for STRUCTURED_EXTRACTION: the job must execute
        against an already-READY version (never the silent no-op path)."""
        factory = extraction_env
        org, user = await seed_extraction_org(factory)
        doc = await seed_contract_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            extraction = await ExtractionService.create_run(
                user=user,
                document_version_id=doc["version_id"],
                schema_key="standard_v1",
                db=session,
            )
            job = await JobService.create_for_extraction(
                session,
                organization_id=org.id,
                document_version_id=doc["version_id"],
                extraction_id=extraction.id,
            )
            await session.commit()

        status = await _run_job_now(factory, job.id)
        assert status == "COMPLETED"

        async with factory() as session:
            repo = DocumentExtractionRepository(session)
            done = await repo.get_by_id(extraction.id)
            assert done is not None
            assert done.status == "COMPLETED"

    async def test_chat_reuse_latest_completed(
        self, extraction_env: async_sessionmaker
    ):
        """§2.6 point 5: the chat path reuses the latest COMPLETED run and
        never creates a new one while a good run exists."""
        factory = extraction_env
        org, user = await seed_extraction_org(factory)
        doc = await seed_contract_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            repo = DocumentExtractionRepository(session)
            assert await repo.get_latest_completed_for_version(doc["version_id"]) is None

            first, created = await ExtractionService.get_or_create_run_from_chat(
                user=user, document_version_id=doc["version_id"], db=session
            )
            assert created is True
            await repo.update_status(first, "COMPLETED", model="stub-llm")
            await session.commit()

            second, created_again = (
                await ExtractionService.get_or_create_run_from_chat(
                    user=user, document_version_id=doc["version_id"], db=session
                )
            )
            assert created_again is False
            assert second.id == first.id
