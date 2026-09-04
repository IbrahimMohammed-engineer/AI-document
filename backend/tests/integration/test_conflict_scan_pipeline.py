"""
Integration tests — the conflict background-scan pipeline (Phase 13, §27.5,
§27.7 — the primary Phase 13 acceptance suite).

Runs against testcontainers PostgreSQL + Redis using the same harness as
test_comparison_pipeline.py: the Arq loop is NOT started —
``run_processing_job`` / ``ConflictService.run_scan`` are invoked directly.

Covers the §27.7 end-to-end fixture (HR Policy A vs HR Policy B):
  - full scan → exactly one conflict, BACKGROUND_SCAN, correct severity
    (confidence 0.9, 2 statements, non-critical → MODERATE) and the LLM's
    topic; both statements carry real chunk/version/page provenance;
  - re-running the scan creates NO duplicate (any status);
  - resolve → REVIEWED + CONFLICT_RESOLVED audit row; a further scan never
    reopens it and never duplicates;
  - a superseded third document is excluded from the candidate pool entirely
    (no phantom conflicts);
  - crash-and-resume (per-document checkpoint) without duplicates;
  - retry exhaustion marks the job FAILED with error_message;
  - the trigger-level in-flight guard prevents two concurrent scans per org.

Requires Docker (testcontainers).
"""
from __future__ import annotations

import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.domain.conflict_rules import classify_conflict_severity
from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.repositories.conflict_repository import ConflictRepository
from app.repositories.processing_job_repository import ProcessingJobRepository
from app.services.conflict_service import ConflictService
from app.workers.jobs import run_processing_job, trigger_conflict_scans
from tests.fixtures.conflict_fixtures import (
    HR_POLICY_A_TEXT,
    HR_POLICY_B_TEXT,
    _pair_vector,
    _unit_vector,
    install_conflict_llm_stub,
    seed_conflict_corpus,
    seed_conflict_document,
    seed_conflict_org,
    set_chunk_embedding,
)
from datetime import date as _date

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
async def scan_env(
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
    install_conflict_llm_stub()

    yield app_session_factory

    await _clean_tables(app_session_factory)
    await pool.aclose()
    set_llm_provider(None)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _create_scan_job(factory, organization_id: str) -> str:
    """Trigger-level job creation (the cron's exact behavior)."""
    from app.services.job_service import JobService

    async with factory() as session:
        job = await JobService.create_for_org_scan(
            session, organization_id=organization_id
        )
        await session.commit()
        return job.id


async def _conflicts(factory):
    async with factory() as session:
        repo = ConflictRepository(session)
        rows = await repo.list_for_org(
            (await _single_org(factory)),
            status=None,
            severity=None,
            requesting_user_id=str(uuid.uuid4()),  # no private docs seeded — no filtering
        )
        for row in rows:
            session.expunge(row)
        return rows


async def _single_org(factory) -> str:
    async with factory() as session:
        row = await session.execute(text("SELECT id FROM organizations LIMIT 1"))
        return str(row.scalar_one())


async def _statements(factory, conflict_id: str):
    async with factory() as session:
        repo = ConflictRepository(session)
        rows = await repo.list_statements(conflict_id)
        for row in rows:
            session.expunge(row)
        return rows


async def _conflict_count(factory) -> int:
    async with factory() as session:
        return (await session.execute(
            text("SELECT count(*) FROM conflicts")
        )).scalar_one()


async def _get_job(factory, job_id: str):
    async with factory() as session:
        job = await ProcessingJobRepository(session).get_by_id(job_id)
        assert job is not None
        session.expunge(job)
        return job


# ── §27.7: the end-to-end fixture ─────────────────────────────────────────────

@pytest.mark.integration
class TestConflictScanPipeline:

    @pytest.mark.asyncio
    async def test_full_scan_creates_exactly_one_conflict_with_evidence(
        self, scan_env
    ):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id

        job_id = await _create_scan_job(factory, org_id)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        # Exactly one conflict with the right shape
        assert await _conflict_count(factory) == 1
        conflicts = await _conflicts(factory)
        conflict = conflicts[0]
        assert conflict.detection_method == "BACKGROUND_SCAN"
        assert conflict.status == "OPEN"
        assert conflict.topic == "Vacation Approval Authority"

        # Severity: confidence 0.9, 2 statements, non-critical
        # → classify_conflict_severity → MODERATE
        expected = classify_conflict_severity(
            is_critical_section=False, confidence=0.9, statement_count=2
        )
        assert conflict.severity == expected
        assert conflict.severity == "MODERATE"

        # Exactly two statements, each with REAL provenance
        statements = await _statements(factory, conflict.id)
        assert len(statements) == 2
        statement_texts = {s.statement_text for s in statements}
        assert statement_texts == {HR_POLICY_A_TEXT, HR_POLICY_B_TEXT}
        chunk_ids = {corpus["policy_a"]["chunk_id"], corpus["policy_b"]["chunk_id"]}
        assert {s.chunk_id for s in statements} == chunk_ids
        for s in statements:
            assert s.chunk_id and s.document_version_id and s.page_id
            assert s.page_number == 1
            assert s.effective_date == _date(2025, 1, 1)
        # The projection side's label carries the section number ("1 …");
        # the semantic_search side's carries the bare title — which is which
        # depends on the documents' UUID iteration order.
        sections = {s.section for s in statements}
        assert len(sections) == 2
        assert any(s and "Vacation Approval" in s for s in sections)
        assert any(s and "Leave Procedures" in s for s in sections)

        # The job checkpointed its counters
        job = await _get_job(factory, job_id)
        assert job.checkpoint is not None
        assert job.checkpoint["documents_scanned"] == 2
        assert job.checkpoint["conflicts_created"] == 1
        assert job.checkpoint["candidates_evaluated"] >= 1
        assert job.document_version_id is None  # org-wide job shape

    @pytest.mark.asyncio
    async def test_rescan_never_duplicates(self, scan_env):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id

        job_id = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id)
        assert await _conflict_count(factory) == 1

        # Second nightly scan — fully idempotent
        job_id2 = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id2)
        assert await _conflict_count(factory) == 1

    @pytest.mark.asyncio
    async def test_resolved_conflict_never_reopens_or_duplicates(self, scan_env):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id

        job_id = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id)
        conflict = (await _conflicts(factory))[0]

        # Resolve (reviewer = the org's admin user)
        async with factory() as session:
            resolved = await ConflictService.resolve(
                conflict.id,
                user=corpus["user"],
                decision="REVIEWED",
                note="Confirmed — policy B is outdated.",
                db=session,
            )
        assert resolved.status == "REVIEWED"
        assert resolved.resolved_by == corpus["user"].id
        assert resolved.resolved_at is not None

        # Audit row exists
        async with factory() as session:
            audits = (await session.execute(text(
                "SELECT action, resource_type, resource_id FROM audit_logs "
                "WHERE action = 'CONFLICT_RESOLVED'"
            ))).all()
        assert len(audits) == 1
        assert str(audits[0][2]) == str(conflict.id)

        # Re-scan: no reopen, no duplicate
        job_id2 = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id2)
        assert await _conflict_count(factory) == 1
        conflicts = await _conflicts(factory)
        assert str(conflicts[0].id) == str(conflict.id)
        assert conflicts[0].status == "REVIEWED"

    @pytest.mark.asyncio
    async def test_superseded_version_never_enters_candidate_pool(self, scan_env):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id

        # Third document whose CURRENT (v2) version is innocuous, but whose
        # SUPERSEDED v1 directly contradicts policy A (high-similarity vector).
        from tests.fixtures.comparison_fixtures import (
            SectionSpec,
            seed_document_with_versions,
        )

        _doc_c, c_seeds = await seed_document_with_versions(
            factory,
            organization_id=org_id,
            owner_id=corpus["user"].id,
            name="HR Policy C",
            versions=[
                (1, _date(2024, 1, 1), [
                    SectionSpec("1", "Vacation Approval",
                                "Vacation requests must be approved by the department head."),
                ]),
                (2, _date(2026, 1, 1), [
                    SectionSpec("1", "Vacation Approval",
                                "Policy C version two carries unrelated content."),
                ]),
            ],
        )
        superseded_version_id = c_seeds[1].version_id
        current_version_id = c_seeds[2].version_id

        async with factory() as session:
            await set_chunk_embedding(
                session, c_seeds[1].chunks["1"], _pair_vector(0, 2, cos=0.95)
            )
            await set_chunk_embedding(
                session, c_seeds[2].chunks["1"], _unit_vector(10)
            )
            await session.commit()

        job_id = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id)

        conflicts = await _conflicts(factory)
        # Only the A-vs-B conflict exists; the superseded v1 of C produced
        # no phantom conflict.
        assert len(conflicts) == 1
        statements = await _statements(factory, conflicts[0].id)
        used_versions = {str(s.document_version_id) for s in statements}
        assert superseded_version_id not in used_versions
        assert used_versions == {
            corpus["policy_a"]["version_id"],
            corpus["policy_b"]["version_id"],
        }
        assert current_version_id not in used_versions  # v2 is unrelated

    @pytest.mark.asyncio
    async def test_priority_becomes_likely_resolved_when_side_superseded(
        self, scan_env
    ):
        """§12: priority is computed LIVE at read time — publishing a newer
        version for one side flips ACTIVE → LIKELY_RESOLVED without any
        database write to the conflict row."""
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id
        admin = corpus["user"]

        job_id = await _create_scan_job(factory, org_id)
        await run_processing_job({}, job_id)

        async with factory() as session:
            conflict = (await session.execute(text(
                "SELECT id FROM conflicts WHERE status = 'OPEN' LIMIT 1"
            ))).first()
        conflict_id = str(conflict[0])

        # While both sides are CURRENT → ACTIVE
        async with factory() as session:
            summaries = await ConflictService.list_conflicts(
                organization_id=org_id, requesting_user=admin,
                status=None, severity=None, db=session,
            )
        assert summaries[0]["priority"] == "ACTIVE"

        # Publish a newer, already-effective version of Policy A → side A is
        # now SUPERSEDED (v1 remains READY but loses the resolve_current_version
        # contest to the later-effective v2).
        async with factory() as session:
            await session.execute(text(
                "INSERT INTO document_versions (id, document_id, version_number, "
                "storage_key, mime_type, file_size_bytes, status, effective_date, "
                "created_by) VALUES (gen_random_uuid(), "
                "(SELECT document_id FROM document_versions WHERE id = :va), "
                "2, 'k', 'application/pdf', 1, 'READY', '2026-01-01', :owner)"
            ), {"va": corpus["policy_a"]["version_id"], "owner": admin.id})
            await session.commit()

        async with factory() as session:
            summaries = await ConflictService.list_conflicts(
                organization_id=org_id, requesting_user=admin,
                status=None, severity=None, db=session,
            )
        assert summaries[0]["priority"] == "LIKELY_RESOLVED"

        # The row itself was never touched (classification is never stored)
        async with factory() as session:
            row = (await session.execute(text(
                "SELECT status FROM conflicts WHERE id = :cid"
            ), {"cid": conflict_id})).first()
        assert row[0] == "OPEN"

    @pytest.mark.asyncio
    async def test_org_with_no_embedded_corpus_completes_clean(self, scan_env):
        factory = scan_env
        org, user = await seed_conflict_org(factory)
        # A document with NO embedding yet (still processing upstream)
        await seed_conflict_document(
            factory,
            organization_id=org.id,
            owner_id=user.id,
            name="Bare Document",
            section_number="1",
            section_title="Intro",
            content="No embeddings here.",
            embedding=None,
        )
        job_id = await _create_scan_job(factory, org.id)
        assert await run_processing_job({}, job_id) == "COMPLETED"
        assert await _conflict_count(factory) == 0
        job = await _get_job(factory, job_id)
        assert job.checkpoint["conflicts_created"] == 0


# ── §27.5: resilience ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestConflictScanResilience:

    @pytest.mark.asyncio
    async def test_crash_mid_scan_resumes_without_duplicates(
        self, scan_env, monkeypatch
    ):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id
        job_id = await _create_scan_job(factory, org_id)

        # Crash while processing the SECOND document: by then the first
        # document's checkpoint (cursor=policy A) is already committed, so
        # the resumed run starts from policy B.
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        real_refs = DocumentChunkRepository.list_embedded_chunk_refs_for_version
        refs_calls = {"n": 0}

        async def crashing_refs(self, document_version_id):
            refs_calls["n"] += 1
            if refs_calls["n"] == 2:
                raise RuntimeError("simulated crash during document 2")
            return await real_refs(self, document_version_id)

        monkeypatch.setattr(
            DocumentChunkRepository,
            "list_embedded_chunk_refs_for_version",
            crashing_refs,
        )
        status = await run_processing_job({}, job_id)
        assert status == "RETRYING"

        # Targeted restore (monkeypatch.undo() would also undo the fixture's
        # queue/db bindings).
        monkeypatch.setattr(
            DocumentChunkRepository,
            "list_embedded_chunk_refs_for_version",
            real_refs,
        )
        status = await run_processing_job({}, job_id)
        assert status == "COMPLETED"

        # Exactly one conflict, no duplicates from the resumed run
        assert await _conflict_count(factory) == 1
        job = await _get_job(factory, job_id)
        # Both documents accounted for across the crashed + resumed runs
        # (the resume started AFTER the committed cursor, never re-scanned
        # the fully-processed first document).
        assert job.checkpoint["documents_scanned"] == 2
        assert job.checkpoint["cursor_document_id"] is not None

    @pytest.mark.asyncio
    async def test_retry_exhaustion_marks_job_failed(self, scan_env, monkeypatch):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)
        org_id = corpus["org"].id
        job_id = await _create_scan_job(factory, org_id)

        async with factory() as session:
            await session.execute(text(
                "UPDATE processing_jobs SET max_attempts = 1 WHERE id = :id"
            ), {"id": job_id})
            await session.commit()

        async def always_crash(*, organization_id, job, db, provider=None):
            raise RuntimeError("permanent scan failure")

        monkeypatch.setattr(ConflictService, "run_scan", staticmethod(always_crash))
        status = await run_processing_job({}, job_id)
        assert status == "FAILED"

        job = await _get_job(factory, job_id)
        assert job.status == "FAILED"
        assert "permanent scan failure" in (job.error_message or "")

    @pytest.mark.asyncio
    async def test_trigger_prevents_two_inflight_scans(self, scan_env):
        factory = scan_env
        corpus = await seed_conflict_corpus(factory)

        # First trigger creates one job
        assert await trigger_conflict_scans({}) == 1
        # Second trigger sees the in-flight PENDING job and skips
        assert await trigger_conflict_scans({}) == 0

        async with factory() as session:
            jobs = (await session.execute(text(
                "SELECT count(*) FROM processing_jobs WHERE job_type = 'CONFLICT_SCAN'"
            ))).scalar_one()
        assert jobs == 1
