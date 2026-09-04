"""
Integration tests — conflict dedup/merge (Phase 13, §14, §27.2; plan §23
Task 7's persistence matrix).

Covers:
  - exact-pair dedup across ALL statuses (OPEN / REVIEWED / DISMISSED) —
    a recorded pair is never re-persisted, never reopened;
  - single-chunk growth applies to OPEN conflicts only — a chunk shared
    with a RESOLVED conflict yields a NEW conflict (the resolution is
    preserved untouched);
  - a pair spanning two distinct OPEN conflicts is logged and skipped
    (V1 does not merge conflict graphs);
  - the concurrent-insert race resolves via the IntegrityError
    catch-and-refetch (unique constraint as backstop), never a 500.

Requires Docker (testcontainers).
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.llm import set_llm_provider
from app.rag.conflict_parsing import ContradictionResult
from app.repositories.conflict_repository import ConflictRepository
from app.services.conflict_service import ConflictService, StatementChunk
from tests.fixtures.conflict_fixtures import seed_conflict_org

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
async def dedup_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    yield app_session_factory
    await _clean_tables(app_session_factory)
    await pool.aclose()
    set_llm_provider(None)


# ── Helpers ───────────────────────────────────────────────────────────────────

_DETECTION = ContradictionResult(
    is_conflict=True,
    confidence=0.9,
    reason="test",
    conflict_topic="Test Topic",
)


async def _side(factory, org_id: str, owner_id: str, label: str) -> StatementChunk:
    """A REAL document/version/page/chunk row set (the provenance FKs are
    NOT NULL + RESTRICT — statements must reference actual rows)."""
    from tests.fixtures.conflict_fixtures import seed_conflict_document

    seed = await seed_conflict_document(
        factory,
        organization_id=org_id,
        owner_id=owner_id,
        name=f"Doc {label}",
        section_number="1",
        section_title=f"Section {label}",
        content=f"Statement text {label}",
        embedding=None,
    )
    return StatementChunk(
        chunk_id=seed["chunk_id"],
        document_id=seed["document_id"],
        document_version_id=seed["version_id"],
        page_id=seed["page_id"],
        page_number=1,
        section_title=f"Section {label}",
        content=f"Statement text {label}",
    )


async def _persist(factory, org_id, a, b, *, method="BACKGROUND_SCAN"):
    async with factory() as session:
        conflict = await ConflictService.persist_or_merge(
            organization_id=org_id,
            chunk_a=a,
            chunk_b=b,
            detection_result=_DETECTION,
            detection_method=method,
            db=session,
        )
        await session.commit()
        return conflict


async def _resolve(factory, conflict_id: str, status: str, resolved_by: str) -> None:
    async with factory() as session:
        repo = ConflictRepository(session)
        conflict = await repo.get_by_id(conflict_id)
        assert conflict is not None
        await repo.resolve(
            conflict, status=status, resolved_by=resolved_by,
            resolution_note=None,
        )
        await session.commit()


async def _count(factory) -> int:
    async with factory() as session:
        return (await session.execute(
            text("SELECT count(*) FROM conflicts")
        )).scalar_one()


# ── The §16.4 persistence matrix ──────────────────────────────────────────────

@pytest.mark.integration
class TestConflictDedupMatrix:

    @pytest.mark.asyncio
    async def test_creates_two_statement_conflict(self, dedup_env):
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")

        conflict = await _persist(factory, org.id, a, b)
        assert conflict is not None
        assert conflict.status == "OPEN"
        assert conflict.detection_method == "BACKGROUND_SCAN"
        assert conflict.topic == "Test Topic"  # from the LLM result

        async with factory() as session:
            repo = ConflictRepository(session)
            statements = await repo.list_statements(conflict.id)
        assert {s.chunk_id for s in statements} == {a.chunk_id, b.chunk_id}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["OPEN", "REVIEWED", "DISMISSED"])
    async def test_exact_pair_dedup_across_all_statuses(
        self, dedup_env, status: str
    ):
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")

        first = await _persist(factory, org.id, a, b)
        assert first is not None
        if status != "OPEN":
            await _resolve(factory, first.id, status, user.id)

        again = await _persist(factory, org.id, a, b)
        assert again is None  # never re-persisted, never reopened
        assert await _count(factory) == 1

        async with factory() as session:
            repo = ConflictRepository(session)
            refetched = await repo.get_by_id(first.id)
        assert refetched.status == status  # untouched

    @pytest.mark.asyncio
    async def test_growth_applies_to_open_conflicts(self, dedup_env):
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")
        c = await _side(factory, org.id, user.id, "C")

        first = await _persist(factory, org.id, a, b)
        # C disagrees with A → the OPEN conflict GROWS to 3 statements
        grown = await _persist(factory, org.id, a, c)
        assert grown is not None
        assert grown.id == first.id

        async with factory() as session:
            repo = ConflictRepository(session)
            statements = await repo.list_statements(first.id)
        assert len(statements) == 3
        assert {s.chunk_id for s in statements} == {
            a.chunk_id, b.chunk_id, c.chunk_id,
        }
        assert await _count(factory) == 1  # no separate conflict for (A, C)

    @pytest.mark.asyncio
    async def test_resolved_conflicts_are_never_grown(self, dedup_env):
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")
        c = await _side(factory, org.id, user.id, "C")

        first = await _persist(factory, org.id, a, b)
        await _resolve(factory, first.id, "REVIEWED", user.id)

        # C now disagrees with A — the resolved conflict stays untouched;
        # a NEW separate conflict is created for its own review cycle.
        result = await _persist(factory, org.id, a, c)
        assert result is not None
        assert result.id != first.id

        async with factory() as session:
            repo = ConflictRepository(session)
            original = await repo.get_by_id(first.id)
            original_statements = await repo.list_statements(first.id)
        assert original.status == "REVIEWED"
        assert len(original_statements) == 2
        assert await _count(factory) == 2

    @pytest.mark.asyncio
    async def test_pair_spanning_two_open_conflicts_is_skipped(self, dedup_env):
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")
        c = await _side(factory, org.id, user.id, "C")
        d = await _side(factory, org.id, user.id, "D")
        e = await _side(factory, org.id, user.id, "E")

        await _persist(factory, org.id, a, b)  # conflict 1
        await _persist(factory, org.id, c, d)  # conflict 2

        # (A, C): each chunk already belongs to a DIFFERENT open conflict →
        # merging is out of scope for V1 — log + skip, no third conflict.
        result = await _persist(factory, org.id, a, c)
        assert result is None
        assert await _count(factory) == 2

        # (A, E): A is in an open conflict, E is new → grows conflict 1.
        grown = await _persist(factory, org.id, a, e)
        assert grown is not None
        assert await _count(factory) == 2

    @pytest.mark.asyncio
    async def test_concurrent_growth_race_resolves_via_constraint(
        self, dedup_env, monkeypatch
    ):
        """Growth-path race (§14): another worker commits chunk C's statement
        into our growth target between our lookups and our INSERT — the
        unique constraint raises IntegrityError, which persist_or_merge
        catches, rolls back, re-fetches, and returns (never a 500)."""
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        c = await _side(factory, org.id, user.id, "C")

        # Conflict X with statement A, already persisted.
        async with factory() as session:
            repo = ConflictRepository(session)
            conflict = await repo.create(
                organization_id=org.id,
                topic="Race Topic",
                severity="MODERATE",
                detection_method="BACKGROUND_SCAN",
            )
            await repo.add_statement(
                conflict.id,
                document_id=a.document_id,
                document_version_id=a.document_version_id,
                chunk_id=a.chunk_id,
                page_id=a.page_id,
                page_number=1,
                statement_text=a.content,
            )
            await session.commit()
            conflict_id = conflict.id

        # The "other worker" already committed C's statement into X.
        async with factory() as session:
            repo = ConflictRepository(session)
            await repo.add_statement(
                conflict_id,
                document_id=c.document_id,
                document_version_id=c.document_version_id,
                chunk_id=c.chunk_id,
                page_id=c.page_id,
                page_number=1,
                statement_text=c.content,
            )
            await session.commit()

        # Simulate our stale view: the exact-pair lookup misses (the other
        # worker's insert raced past our snapshot) and C's open-conflict
        # lookup misses too — so persist_or_merge takes the GROWTH path.
        real_pair = ConflictRepository.find_by_exact_pair
        real_open = ConflictRepository.find_open_by_single_chunk
        pair_calls = {"n": 0}

        async def racing_pair(self, organization_id, chunk_id_a, chunk_id_b):
            pair_calls["n"] += 1
            if pair_calls["n"] == 1:
                return None  # stale dedup view
            return await real_pair(self, organization_id, chunk_id_a, chunk_id_b)

        async def racing_open(self, organization_id, chunk_id):
            if chunk_id == c.chunk_id:
                return None  # C's statement not yet visible to us
            return await real_open(self, organization_id, chunk_id)

        monkeypatch.setattr(ConflictRepository, "find_by_exact_pair", racing_pair)
        monkeypatch.setattr(
            ConflictRepository, "find_open_by_single_chunk", racing_open
        )
        result = await _persist(factory, org.id, a, c)
        monkeypatch.setattr(ConflictRepository, "find_by_exact_pair", real_pair)
        monkeypatch.setattr(
            ConflictRepository, "find_open_by_single_chunk", real_open
        )

        # Catch-and-refetch returned the existing conflict
        assert result is not None
        assert result.id == conflict_id
        assert await _count(factory) == 1
        async with factory() as session:
            repo = ConflictRepository(session)
            statements = await repo.list_statements(conflict_id)
        assert len(statements) == 2  # no duplicate statement rows

    @pytest.mark.asyncio
    async def test_both_sides_same_open_conflict_is_noop(self, dedup_env):
        """Both chunks already statements of the SAME open conflict (the
        exact-pair query is keyed on the pair — a re-derived pair whose two
        chunks coexist in one conflict but were never queried together)."""
        factory = dedup_env
        org, user = await seed_conflict_org(factory)
        a = await _side(factory, org.id, user.id, "A")
        b = await _side(factory, org.id, user.id, "B")
        c = await _side(factory, org.id, user.id, "C")

        conflict = await _persist(factory, org.id, a, b)
        grown = await _persist(factory, org.id, a, c)
        assert grown.id == conflict.id

        # (B, C): both already statements of the SAME conflict → the
        # exact-pair dedup catches it (membership, not pair history) → no-op.
        result = await _persist(factory, org.id, b, c)
        assert result is None
        assert await _count(factory) == 1
        async with factory() as session:
            repo = ConflictRepository(session)
            assert len(await repo.list_statements(conflict.id)) == 3
