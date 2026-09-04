"""
Integration tests — comparison-derived conflict seeding (Phase 13, §13,
§27.2; plan §23 Task 9).

The single Phase-13 touch-point into Phase-12-owned code: immediately after
``handle_comparison`` sets status = COMPLETED, qualifying changes seed
conflicts through the same dedup/persistence path the background scan uses.

Covers:
  - a MODIFIED + MAJOR change (critical section) between two CURRENT
    versions seeds exactly one COMPARISON_DERIVED conflict — with NO LLM
    contradiction-check call (the deterministic qualification IS the
    confirmation);
  - non-MAJOR (MODERATE/MINOR) changes do not seed;
  - a qualifying change where one side is SUPERSEDED does not seed (it is
    version history, not a live disagreement);
  - re-running the seeding (or re-discovering the same pair) never
    double-seeds (shared exact-pair dedup).

Requires Docker (testcontainers).
"""
from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.services.comparison_service import ComparisonService
from app.workers.jobs import run_processing_job
from tests.fixtures.comparison_fixtures import (
    SectionSpec,
    seed_document_with_versions,
    seed_comparison_document,
)
from tests.fixtures.conflict_fixtures import (
    install_conflict_llm_stub,
    seed_conflict_document,
    seed_conflict_org,
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
async def seeding_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
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

async def _run_comparison(factory, user, version_a_id: str, version_b_id: str) -> str:
    """Create + execute a comparison; return its id (status COMPLETED)."""
    async with factory() as session:
        comparison, created = await ComparisonService.get_or_create_comparison(
            user=user,
            document_a_version_id=version_a_id,
            document_b_version_id=version_b_id,
            db=session,
        )
        assert created is True
        comparison_id = comparison.id
    async with factory() as session:
        row = await session.execute(
            text("SELECT id FROM processing_jobs WHERE comparison_id = :cid"),
            {"cid": comparison_id},
        )
        job_id = str(row.scalar_one())
    assert await run_processing_job({}, job_id) == "COMPLETED"
    return comparison_id


async def _conflict_rows(factory):
    async with factory() as session:
        return (await session.execute(text(
            "SELECT id, topic, severity, detection_method FROM conflicts "
            "ORDER BY detected_at"
        ))).all()


async def _conflict_statements(factory):
    async with factory() as session:
        return (await session.execute(text(
            "SELECT cs.chunk_id, d.name FROM conflict_statements cs "
            "JOIN documents d ON d.id = cs.document_id ORDER BY cs.created_at"
        ))).all()


async def _set_critical_sections(factory, org_id: str, patterns: list[str]) -> None:
    import json

    async with factory() as session:
        await session.execute(text(
            "UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"
        ), {"id": org_id, "s": json.dumps(
            {"comparison": {"critical_sections": patterns}}
        )})
        await session.commit()


# ── The seeding matrix ────────────────────────────────────────────────────────

@pytest.mark.integration
class TestComparisonDerivedSeeding:

    @pytest.mark.asyncio
    async def test_modified_major_between_current_versions_seeds(
        self, seeding_env
    ):
        factory = seeding_env
        org, user = await seed_conflict_org(factory)
        await _set_critical_sections(factory, org.id, ["Approval Process"])

        # Two documents, one CURRENT version each, same section number but
        # different approval windows — the cross-document comparison sees a
        # MODIFIED change in the critical "Approval Process" section.
        doc_a = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy A", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 5 business days.",
            effective_date=date(2025, 1, 1),
        )
        doc_b = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy B", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 7 business days.",
            effective_date=date(2025, 1, 1),
        )

        comparison_id = await _run_comparison(
            factory, user, doc_a["version_id"], doc_b["version_id"]
        )

        conflicts = await _conflict_rows(factory)
        assert len(conflicts) == 1
        _id, topic, severity, detection_method = conflicts[0]
        assert detection_method == "COMPARISON_DERIVED"
        assert severity == "MAJOR"  # inherited from the qualifying change's rule
        assert topic == "Approval Process — Version Discrepancy"

        statements = await _conflict_statements(factory)
        assert len(statements) == 2
        assert {name for _chunk, name in statements} == {"Policy A", "Policy B"}

    @pytest.mark.asyncio
    async def test_non_major_changes_do_not_seed(self, seeding_env):
        factory = seeding_env
        org, user = await seed_conflict_org(factory)
        # NO critical sections → material+small change is MODERATE, not MAJOR

        doc_a = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy A", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 5 business days.",
            effective_date=date(2025, 1, 1),
        )
        doc_b = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy B", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 7 business days.",
            effective_date=date(2025, 1, 1),
        )

        await _run_comparison(
            factory, user, doc_a["version_id"], doc_b["version_id"]
        )
        # The comparison itself completed with a MODERATE change — no seed
        assert await _conflict_rows(factory) == []

    @pytest.mark.asyncio
    async def test_superseded_side_does_not_seed(self, seeding_env):
        factory = seeding_env
        seed = await seed_comparison_document(factory)
        await _set_critical_sections(factory, seed.org.id, ["Approval Process"])

        # The standard fixture compares v2025 vs v2026 of the SAME document —
        # v2025 is SUPERSEDED once v2026 exists, so the qualification rule
        # (both sides CURRENT) blocks seeding entirely.
        comparison_id = await _run_comparison(
            factory, seed.user, seed.version_a_id, seed.version_b_id
        )
        assert await _conflict_rows(factory) == []

        # Even a manual re-invocation stays at zero
        async with factory() as session:
            from app.services.conflict_service import ConflictService

            seeded = await ConflictService.seed_from_comparison(
                comparison_id, session
            )
        assert seeded == 0

    @pytest.mark.asyncio
    async def test_reseeding_never_duplicates(self, seeding_env):
        factory = seeding_env
        org, user = await seed_conflict_org(factory)
        await _set_critical_sections(factory, org.id, ["Approval Process"])

        doc_a = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy A", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 5 business days.",
            effective_date=date(2025, 1, 1),
        )
        doc_b = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy B", section_number="3.1",
            section_title="Approval Process",
            content="Approval must be completed within 7 business days.",
            effective_date=date(2025, 1, 1),
        )
        comparison_id = await _run_comparison(
            factory, user, doc_a["version_id"], doc_b["version_id"]
        )
        assert len(await _conflict_rows(factory)) == 1

        # Idempotent manual re-invocation (§13 — loose coupling by ID)
        async with factory() as session:
            from app.services.conflict_service import ConflictService

            seeded_again = await ConflictService.seed_from_comparison(
                comparison_id, session
            )
        assert seeded_again == 0
        assert len(await _conflict_rows(factory)) == 1

    @pytest.mark.asyncio
    async def test_background_scan_dedups_against_comparison_seed(
        self, seeding_env
    ):
        """A comparison-derived seed and a later background-scan discovery of
        the same chunk pair never double-persist (§13/§14)."""
        factory = seeding_env
        org, user = await seed_conflict_org(factory)
        await _set_critical_sections(factory, org.id, ["Approval Process"])

        content_a = "Approval must be completed within 5 business days."
        content_b = "Approval must be completed within 7 business days."
        doc_a = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy A", section_number="3.1",
            section_title="Approval Process", content=content_a,
            effective_date=date(2025, 1, 1),
            # Same section content as each chunk's own vector space — give
            # both chunks high-similarity embeddings so the SCAN also finds
            # the pair (unit vectors at cos 0.9).
        )
        doc_b = await seed_conflict_document(
            factory, organization_id=org.id, owner_id=user.id,
            name="Policy B", section_number="3.1",
            section_title="Approval Process", content=content_b,
            effective_date=date(2025, 1, 1),
        )
        # Controlled embeddings via the same raw-SQL pattern the fixtures use
        from tests.fixtures.conflict_fixtures import (
            _pair_vector,
            _unit_vector,
            set_chunk_embedding,
        )

        async with factory() as session:
            await set_chunk_embedding(
                session, doc_a["chunk_id"], _unit_vector(0)
            )
            await set_chunk_embedding(
                session, doc_b["chunk_id"], _pair_vector(0, 1, cos=0.9)
            )
            await session.commit()

        # 1. Comparison seeds the conflict first
        await _run_comparison(
            factory, user, doc_a["version_id"], doc_b["version_id"]
        )
        assert len(await _conflict_rows(factory)) == 1

        # 2. A background scan then finds the same pair — dedup no-ops
        from app.services.job_service import JobService

        async with factory() as session:
            job = await JobService.create_for_org_scan(
                session, organization_id=org.id
            )
            await session.commit()
            job_id = job.id
        assert await run_processing_job({}, job_id) == "COMPLETED"
        assert len(await _conflict_rows(factory)) == 1
