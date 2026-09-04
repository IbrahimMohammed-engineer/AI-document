"""
Integration tests — the comparison worker pipeline (Phase 12, plan §16.2/§16.6).

Runs against testcontainers PostgreSQL + Redis using the same harness as
test_extraction_pipeline.py: the Arq loop is NOT started —
``run_processing_job`` is invoked directly against the real containers
(exactly what the worker executes per pointer).

Covers:
  - §16.6 end-to-end fixture: 2025 vs 2026 Marketing Policy → the
    approval-window change is MODIFIED/MODERATE with correct old/new text;
    the added section is ADDED/MODERATE; identical sections produce NO rows;
    summary counts are correct; status=COMPLETED.
  - Re-requesting a completed pair reuses the persisted result (no second
    job enqueued).
  - Crash-and-resume: a simulated crash after N persisted sections leaves
    no duplicate rows after the resumed run (Task 8 point 4).
  - Semantic-LLM failure degrades gracefully: the job still COMPLETES and
    severity falls to the materiality=None branch (§9.6/§9.8).
  - Retry exhaustion: job AND comparison end FAILED with error_message.

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
from app.infrastructure.llm import (
    LLMProviderError,
    StubLLMProvider,
    set_llm_provider,
)
from app.repositories.document_comparison_repository import (
    DocumentComparisonRepository,
)
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.services.comparison_service import ComparisonService
from app.workers.jobs import run_processing_job
from tests.fixtures.comparison_fixtures import (
    V1_SECTIONS,
    seed_comparison_document,
)

# FK-safe delete order — comparison tables precede the document family
# (processing_jobs.comparison_id CASCADE would handle it, but explicit
# order documents the dependency chain, matching conftest).
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
    set_llm_provider(None)


@pytest_asyncio.fixture()
async def material_llm(monkeypatch) -> StubLLMProvider:
    """Semantic-comparison stub answering 'material' for every section."""
    provider = StubLLMProvider(
        responder=lambda messages: '{"materiality": "material", "rationale": "The deadline changed."}'
    )
    set_llm_provider(provider)
    return provider


async def _create_comparison(factory, seed) -> tuple[str, str]:
    """Create the comparison via the service; return (comparison_id, job_id)."""
    async with factory() as session:
        comparison, created = await ComparisonService.get_or_create_comparison(
            user=seed.user,
            document_a_version_id=seed.version_a_id,
            document_b_version_id=seed.version_b_id,
            db=session,
        )
    assert created is True
    job_id = await _job_id_for_comparison(factory, comparison.id)
    return comparison.id, job_id


async def _job_id_for_comparison(factory, comparison_id: str) -> str:
    async with factory() as session:
        row = await session.execute(
            text("SELECT id FROM processing_jobs WHERE comparison_id = :cid"),
            {"cid": comparison_id},
        )
        return str(row.scalar_one())


async def _get_comparison(factory, comparison_id: str):
    async with factory() as session:
        repo = DocumentComparisonRepository(session)
        comparison = await repo.get_by_id(comparison_id)
        assert comparison is not None
        session.expunge(comparison)
        return comparison


async def _get_changes(factory, comparison_id: str):
    async with factory() as session:
        repo = DocumentComparisonRepository(session)
        changes = await repo.list_changes(comparison_id)
        for c in changes:
            session.expunge(c)
        return changes


# ── §16.6: the exit-criterion fixture ─────────────────────────────────────────

@pytest.mark.integration
class TestComparisonPipeline:

    @pytest.mark.asyncio
    async def test_end_to_end_fixture_2025_vs_2026(
        self, worker_env, material_llm
    ):
        factory = worker_env
        seed = await seed_comparison_document(factory)
        comparison_id, job_id = await _create_comparison(factory, seed)

        assert await run_processing_job({}, job_id) == "COMPLETED"

        comparison = await _get_comparison(factory, comparison_id)
        assert comparison.status == "COMPLETED"
        assert comparison.completed_at is not None

        changes = await _get_changes(factory, comparison_id)
        by_section = {c.section: c for c in changes}

        # Exactly the expected rows: 2 MODIFIED + 1 ADDED.  The identical
        # "1 Purpose" section produced NO row (UNCHANGED never persisted).
        assert set(by_section) == {
            "Approval Process", "Review Cycle", "Digital Signatures",
        }

        approval = by_section["Approval Process"]
        assert approval.change_type == "MODIFIED"
        # material + proportion 1/8 ≈ 0.125 < 0.3 → MODERATE (plan §9.8)
        assert approval.severity == "MODERATE"
        assert "5 business days" in approval.old_text
        assert "7 business days" in approval.new_text
        assert approval.old_chunk_id is not None
        assert approval.new_chunk_id is not None
        assert approval.truncated is False

        review = by_section["Review Cycle"]
        assert review.change_type == "MODIFIED"
        assert review.severity == "MODERATE"
        assert "12 months" in review.old_text
        assert "6 months" in review.new_text

        added = by_section["Digital Signatures"]
        assert added.change_type == "ADDED"
        assert added.severity == "MODERATE"  # non-critical ADDED → MODERATE
        assert added.old_chunk_id is None    # ADDED carries no old side
        assert added.old_text is None
        assert added.new_chunk_id is not None

        summary = comparison.summary
        assert summary["total"] == 3
        assert summary["major"] == 0
        assert summary["moderate"] == 3
        assert summary["minor"] == 0
        assert summary["alignment_degraded"] is False

    @pytest.mark.asyncio
    async def test_critical_section_elevates_to_major(self, worker_env, material_llm):
        factory = worker_env
        seed = await seed_comparison_document(factory)
        # Configure the org's critical_sections (§9.8 org-settings convention)
        async with factory() as session:
            await session.execute(text(
                "UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"
            ), {"id": seed.org.id,
                "s": '{"comparison": {"critical_sections": ["Approval Process"]}}'})
            await session.commit()

        comparison_id, job_id = await _create_comparison(factory, seed)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        changes = {c.section: c for c in await _get_changes(factory, comparison_id)}
        assert changes["Approval Process"].severity == "MAJOR"
        # Non-critical sections keep their computed severities
        assert changes["Review Cycle"].severity == "MODERATE"

    @pytest.mark.asyncio
    async def test_identical_versions_produce_zero_changes(
        self, worker_env, material_llm
    ):
        """Byte-identical content → every section UNCHANGED → zero rows,
        status COMPLETED, empty-state summary (plan §20 edge case)."""
        from datetime import date
        from tests.fixtures.comparison_fixtures import seed_document_with_versions

        factory = worker_env
        seed = await seed_comparison_document(factory)
        # A third version byte-identical to v1 (same sections, new date)
        _, seeds = await seed_document_with_versions(
            factory,
            organization_id=seed.org.id,
            owner_id=seed.user.id,
            versions=[(3, date(2027, 1, 1), V1_SECTIONS)],
        )
        v3_id = seeds[3].version_id

        async with factory() as session:
            comparison, created = await ComparisonService.get_or_create_comparison(
                user=seed.user,
                document_a_version_id=seed.version_a_id,
                document_b_version_id=v3_id,
                db=session,
            )
        assert created is True
        job_id = await _job_id_for_comparison(factory, comparison.id)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        comparison = await _get_comparison(factory, comparison.id)
        assert comparison.status == "COMPLETED"
        assert await _get_changes(factory, comparison.id) == []
        assert comparison.summary["total"] == 0

    @pytest.mark.asyncio
    async def test_reuse_after_completion_does_not_recompute(
        self, worker_env, material_llm
    ):
        factory = worker_env
        seed = await seed_comparison_document(factory)
        comparison_id, job_id = await _create_comparison(factory, seed)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        async with factory() as session:
            jobs_before = (await session.execute(
                text("SELECT count(*) FROM processing_jobs")
            )).scalar_one()

        comparison, created = None, None
        async with factory() as session:
            comparison, created = await ComparisonService.get_or_create_comparison(
                user=seed.user,
                document_a_version_id=seed.version_b_id,   # reversed order —
                document_b_version_id=seed.version_a_id,   # still the same pair
                db=session,
            )
        assert created is False
        assert comparison.id == comparison_id

        async with factory() as session:
            jobs_after = (await session.execute(
                text("SELECT count(*) FROM processing_jobs")
            )).scalar_one()
        assert jobs_after == jobs_before  # no second job was enqueued


# ── §16.2: resumability + graceful degradation + retries ──────────────────────

@pytest.mark.integration
class TestComparisonResilience:

    @pytest.mark.asyncio
    async def test_crash_mid_run_resumes_without_duplicates(
        self, worker_env, material_llm, monkeypatch
    ):
        factory = worker_env
        seed = await seed_comparison_document(factory)
        comparison_id, job_id = await _create_comparison(factory, seed)

        # Crash the worker on the SECOND persisted change (the first MODIFIED
        # section is already committed by the time the second add_change runs).
        real_add_change = DocumentComparisonRepository.add_change
        calls = {"n": 0}

        async def crashing_add_change(self, comparison_id, **fields):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated worker crash mid-comparison")
            return await real_add_change(self, comparison_id, **fields)

        monkeypatch.setattr(
            DocumentComparisonRepository, "add_change", crashing_add_change
        )
        status = await run_processing_job({}, job_id)
        assert status == "RETRYING"

        monkeypatch.setattr(
            DocumentComparisonRepository, "add_change", real_add_change
        )
        # Resume: run_processing_job accepts a RETRYING pointer (real retry
        # semantics — the sweep/requeue path re-runs the same function).
        status = await run_processing_job({}, job_id)
        assert status == "COMPLETED"

        changes = await _get_changes(factory, comparison_id)
        sections = [c.section for c in changes]
        assert sorted(sections) == [
            "Approval Process", "Digital Signatures", "Review Cycle",
        ]
        assert len(sections) == len(set(sections))  # zero duplicates

        comparison = await _get_comparison(factory, comparison_id)
        assert comparison.status == "COMPLETED"
        assert comparison.summary["total"] == 3

    @pytest.mark.asyncio
    async def test_semantic_llm_failure_degrades_not_fails(
        self, worker_env
    ):
        """§9.6/§9.8: an LLM outage only removes materiality — severity uses
        the None branch (proportion 0.125 < 0.5 → MINOR); the job COMPLETES."""
        factory = worker_env
        seed = await seed_comparison_document(factory)

        class _RaisingProvider(StubLLMProvider):
            async def generate(self, *args, **kwargs):  # type: ignore[override]
                raise LLMProviderError("provider down", code="LLM_ERROR")

        set_llm_provider(_RaisingProvider())

        comparison_id, job_id = await _create_comparison(factory, seed)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        changes = {c.section: c for c in await _get_changes(factory, comparison_id)}
        approval = changes["Approval Process"]
        assert approval.severity == "MINOR"  # None-branch: <0.5 → MINOR
        comparison = await _get_comparison(factory, comparison_id)
        assert comparison.status == "COMPLETED"
        assert comparison.error_message is None

    @pytest.mark.asyncio
    async def test_retry_exhaustion_marks_job_and_comparison_failed(
        self, worker_env, material_llm, monkeypatch
    ):
        factory = worker_env
        seed = await seed_comparison_document(factory)
        comparison_id, job_id = await _create_comparison(factory, seed)

        # Spend the retry budget: a single attempt, always-crashing persist
        async with factory() as session:
            await session.execute(text(
                "UPDATE processing_jobs SET max_attempts = 1 WHERE id = :id"
            ), {"id": job_id})
            await session.commit()

        async def always_crash(self, comparison_id, **fields):
            raise RuntimeError("permanent comparison failure")

        monkeypatch.setattr(
            DocumentComparisonRepository, "add_change", always_crash
        )
        status = await run_processing_job({}, job_id)
        assert status == "FAILED"

        job = await _get_job(factory, job_id)
        assert job.status == "FAILED"
        assert "permanent comparison failure" in (job.error_message or "")

        comparison = await _get_comparison(factory, comparison_id)
        assert comparison.status == "FAILED"
        assert "Retries exhausted" in (comparison.error_message or "")

    @pytest.mark.asyncio
    async def test_not_ready_version_rejected_at_creation(self, worker_env):
        """Either version not READY → 422 at request time (plan §9.3)."""
        from app.core.exceptions import ValidationError
        from datetime import date
        from tests.fixtures.comparison_fixtures import (
            SectionSpec,
            seed_document_with_versions,
        )

        factory = worker_env
        seed = await seed_comparison_document(factory)
        pending = SectionSpec("9", "Pending Section", "Still processing content.")
        _, seeds = await seed_document_with_versions(
            factory,
            organization_id=seed.org.id,
            owner_id=seed.user.id,
            versions=[(3, date(2027, 1, 1), [pending])],
        )
        # Force the new version out of READY
        async with factory() as session:
            await session.execute(text(
                "UPDATE document_versions SET status = 'CHUNKING' WHERE id = :id"
            ), {"id": seeds[3].version_id})
            await session.commit()

        with pytest.raises(ValidationError):
            async with factory() as session:
                await ComparisonService.get_or_create_comparison(
                    user=seed.user,
                    document_a_version_id=seed.version_a_id,
                    document_b_version_id=seeds[3].version_id,
                    db=session,
                )


async def _get_job(factory, job_id: str):
    async with factory() as session:
        job = await ProcessingJobRepository(session).get_by_id(job_id)
        assert job is not None
        session.expunge(job)
        return job
