"""
Integration tests — the summary pipeline (Phase 14, plan §8.3).

Same harness as test_conflict_scan_pipeline.py: the Arq loop is NOT started —
``run_processing_job`` / ``SummaryService.run`` are invoked directly.

Covers:
  - full pipeline via the worker dispatch (§7.1): fixture document →
    get_or_create_summary → run_processing_job → COMPLETED summary whose
    every surviving item carries citations resolving to REAL chunks;
  - THE DISPATCH-TRAP REGRESSION TEST (§2.3/§8.3, the single most important
    new test in the plan): a SUMMARY job against an ALREADY-READY version
    must actually execute the handler and reach COMPLETED — not be silently
    marked complete by the per-version pipeline path's READY-short-circuit;
  - long-document sampling: sampling.sampled=True + section-diverse
    coverage disclosed;
  - sampling disclosure persisted with strategy "full" for short documents.

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
from app.infrastructure.llm import set_llm_provider
from app.repositories.document_summary_repository import DocumentSummaryRepository
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.services.job_service import JobService
from app.services.summary_service import SummaryService
from app.workers.jobs import run_processing_job
from tests.fixtures.summary_fixtures import (
    install_summary_llm_stub,
    seed_summary_document,
    seed_summary_org,
)

# FK-safe delete order (mirrors conftest)
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
async def summary_env(
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
    install_summary_llm_stub()

    yield app_session_factory

    await _clean_tables(app_session_factory)
    await pool.aclose()
    set_llm_provider(None)


async def _run_job_now(factory: async_sessionmaker, job_id: str) -> str:
    """Claim+execute one job directly (the worker's exact entry point)."""
    from app.repositories.processing_job_repository import ProcessingJobRepository

    async with factory() as session:
        job = await ProcessingJobRepository(session).get_by_id(job_id)
        assert job is not None
        job.status = "PENDING"
        await session.commit()
    return await run_processing_job({}, job_id)


@pytest.mark.integration
class TestSummaryPipeline:

    async def test_full_pipeline_completes_with_cited_items(
        self, summary_env: async_sessionmaker
    ):
        """§7.1 end-to-end (worker dispatch, not the service alone): the
        persisted summary's every surviving item resolves to a real chunk."""
        factory = summary_env
        org, user = await seed_summary_org(factory)
        doc = await seed_summary_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            summary, created = await SummaryService.get_or_create_summary(
                user=user, document_version_id=doc["version_id"], db=session
            )
            assert created is True
        assert summary.status == "PENDING"

        # The job row exists and is paired (CK-enforced).
        async with factory() as session:
            job = await ProcessingJobRepository(session).get_by_id(
                (
                    await session.execute(
                        text("SELECT id FROM processing_jobs "
                             "WHERE summary_id = :sid"),
                        {"sid": summary.id},
                    )
                ).scalar_one()
            )
            assert job.job_type == "SUMMARY"

        status = await _run_job_now(factory, job.id)
        assert status == "COMPLETED"

        async with factory() as session:
            repo = DocumentSummaryRepository(session)
            done = await repo.get_by_version(doc["version_id"])
            assert done is not None and done.status == "COMPLETED"
            assert done.summary is not None
            # Exit criterion 1: every surviving bullet resolves to a real,
            # retrievable chunk.
            chunk_ids = {
                citation["chunk_id"]
                for field in done.summary.values()
                if isinstance(field, list)
                for item in field
                if isinstance(item, dict)
                for citation in item.get("citations", [])
            }
            assert chunk_ids, "summary items must carry citations"
            rows = (
                await session.execute(
                    text("SELECT id FROM document_chunks"),
                )
            ).fetchall()
            real_chunk_ids = {row[0] for row in rows}
            assert chunk_ids <= real_chunk_ids
            # Short fixture → full strategy, not sampled.
            assert done.sampling is not None
            assert done.sampling["sampled"] is False
            assert done.sampling["strategy"] == "full"

    async def test_ready_version_dispatch_trap_regression(
        self, summary_env: async_sessionmaker
    ):
        """§2.3/§8.3 — THE critical regression: a SUMMARY job against an
        already-READY version must EXECUTE its handler (not be silently
        marked complete by the per-version path's READY short-circuit,
        which is every real invocation)."""
        factory = summary_env
        org, user = await seed_summary_org(factory)
        doc = await seed_summary_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            summary, _created = await SummaryService.get_or_create_summary(
                user=user, document_version_id=doc["version_id"], db=session
            )
            job = await JobService.create_for_summary(
                session,
                organization_id=org.id,
                document_version_id=doc["version_id"],
                summary_id=summary.id,
            )
            await session.commit()

        status = await _run_job_now(factory, job.id)
        assert status == "COMPLETED"

        # The handler RAN: the domain row reached COMPLETED with content —
        # the silent no-op failure mode would leave it PENDING with no JSONB.
        async with factory() as session:
            repo = DocumentSummaryRepository(session)
            done = await repo.get_by_id(summary.id)
            assert done is not None
            assert done.status == "COMPLETED"
            assert done.summary is not None

    async def test_long_document_sampling_disclosed(
        self, summary_env: async_sessionmaker
    ):
        """Exit criterion 7: sampling is DISCLOSED, never silent — a
        long-document fixture produces sampling.sampled=True with
        section-diverse coverage across every top-level section."""
        factory = summary_env
        org, user = await seed_summary_org(factory)
        # 3 sections x 2 chunks x 1200 tokens = 7200 > the 12000 default? No —
        # use huge tokens to force sampling regardless of budget tuning.
        sections = [
            {"title": f"Section {n}", "number": str(n),
             "content": f"Section {n} content. " * 20, "tokens": 9000}
            for n in (1, 2, 3)
        ]
        doc = await seed_summary_document(
            factory, organization_id=org.id, owner_id=user.id, sections=sections
        )

        async with factory() as session:
            summary, _created = await SummaryService.get_or_create_summary(
                user=user, document_version_id=doc["version_id"], db=session
            )
            job = await JobService.create_for_summary(
                session,
                organization_id=org.id,
                document_version_id=doc["version_id"],
                summary_id=summary.id,
            )
            await session.commit()

        status = await _run_job_now(factory, job.id)
        assert status == "COMPLETED"

        async with factory() as session:
            repo = DocumentSummaryRepository(session)
            done = await repo.get_by_version(doc["version_id"])
            assert done is not None
            assert done.sampling is not None
            assert done.sampling["sampled"] is True
            assert done.sampling["strategy"] == "section_diverse"
            # Section coverage: every top-level section contributed.
            assert set(done.sampling["included_section_ids"]) == set(
                doc["section_ids"]
            )

    async def test_provider_failure_marks_summary_failed(
        self, summary_env: async_sessionmaker, monkeypatch
    ):
        """§5.14: an unavailable provider at generation time fails the job —
        the summary row shows FAILED (visible mid-retry) rather than garbage."""
        from app.infrastructure import llm as llm_mod

        factory = summary_env
        org, user = await seed_summary_org(factory)
        doc = await seed_summary_document(
            factory, organization_id=org.id, owner_id=user.id
        )

        async with factory() as session:
            summary, _created = await SummaryService.get_or_create_summary(
                user=user, document_version_id=doc["version_id"], db=session
            )
            job = await JobService.create_for_summary(
                session,
                organization_id=org.id,
                document_version_id=doc["version_id"],
                summary_id=summary.id,
            )
            await session.commit()

        # No provider configured → RuntimeError inside the pipeline.
        monkeypatch.setattr(llm_mod, "_provider", None, raising=False)

        status = await _run_job_now(factory, job.id)
        # Job retries exhausted or retrying — either way NOT completed.
        assert status in ("FAILED", "RETRYING")

        async with factory() as session:
            repo = DocumentSummaryRepository(session)
            row = await repo.get_by_version(doc["version_id"])
            assert row is not None
            assert row.status in ("FAILED", "PENDING")
            if row.status == "FAILED":
                assert row.error_message
