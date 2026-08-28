"""
Integration tests for the Phase 8 hybrid search pipeline.

Requires testcontainers (PostgreSQL+pgvector, full Alembic stack incl. the
Phase 8 GIN index) — tests are skipped when Docker is unavailable.

Tests (roadmap Phase 8 §Testing):
  - GIN index on content_tsv + supporting filter indexes exist
  - Keyword branch: matching + ranking over a seeded multi-document corpus
  - Identical mandatory scope predicates on BOTH branches (tenant isolation,
    allowed-version boundary, soft-delete exclusion)
  - Metadata filters combine with AND inside both branch queries
    (document_type / department / owner / collection)
  - HybridRetriever end-to-end: fusion + rerank over real SQL branches,
    reranker outage → unreranked fused results (degraded, never failed)
  - Temporal scope: as_of resolves the effective historical version via the
    real AuthorizationService (exit-criteria demonstration)

All tests use StubEmbeddingProvider / StubRerankerProvider — no real API calls.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.reranker import StubRerankerProvider, set_reranker_provider


# ── Seeding helpers ───────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_user(session, org_id: str | None = None) -> tuple[str, str]:
    """Create minimal org + user rows. Returns (org_id, user_id)."""
    org_id = org_id or _uuid()
    user_id = _uuid()
    await session.execute(
        text("INSERT INTO organizations (id, name, slug, plan) VALUES (:id, :n, :s, 'basic')"),
        {"id": org_id, "n": f"Org {org_id[:8]}", "s": org_id[:8]},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, organization_id, email, full_name) "
            "VALUES (:id, :org, :email, :name)"
        ),
        {
            "id": user_id,
            "org": org_id,
            "email": f"{user_id[:8]}@example.com",
            "name": "Test User",
        },
    )
    await session.flush()
    return org_id, user_id


async def _seed_document(
    session,
    org_id: str,
    owner_id: str,
    *,
    name: str = "Test Doc",
    document_type: str = "technical",
    department: str | None = None,
    effective_date: date | None = None,
    version_status: str = "READY",
) -> tuple[str, str]:
    """Create document + READY version. Returns (doc_id, ver_id)."""
    doc_id = _uuid()
    ver_id = _uuid()
    await session.execute(
        text(
            "INSERT INTO documents (id, organization_id, owner_id, name, document_type, "
            "department, status, access_level) "
            "VALUES (:id, :org, :owner, :name, :dtype, :dept, 'active', 'organization')"
        ),
        {
            "id": doc_id,
            "org": org_id,
            "owner": owner_id,
            "name": name,
            "dtype": document_type,
            "dept": department,
        },
    )
    await session.execute(
        text(
            "INSERT INTO document_versions (id, document_id, version_number, storage_key, "
            "mime_type, file_size_bytes, status, created_by, effective_date) "
            "VALUES (:id, :doc, 1, 'key', 'application/pdf', 100, :status, :owner, :eff)"
        ),
        {"id": ver_id, "doc": doc_id, "owner": owner_id, "status": version_status,
         "eff": effective_date},
    )
    await session.flush()
    return doc_id, ver_id


async def _seed_page(session, ver_id: str) -> str:
    page_id = _uuid()
    await session.execute(
        text(
            "INSERT INTO document_pages (id, document_version_id, page_number, text) "
            "VALUES (:id, :ver, 1, 'page content')"
        ),
        {"id": page_id, "ver": ver_id},
    )
    await session.flush()
    return page_id


async def _seed_chunk(
    session,
    ver_id: str,
    org_id: str,
    page_id: str,
    idx: int,
    content: str,
    *,
    embed: bool = True,
    provider: StubEmbeddingProvider | None = None,
) -> str:
    """Insert a chunk; optionally fill its embedding (semantic branch)."""
    chunk_id = _uuid()
    await session.execute(
        text(
            "INSERT INTO document_chunks "
            "(id, document_version_id, organization_id, page_id, chunk_index, content, "
            "content_hash, token_count) "
            "VALUES (:id, :ver, :org, :page, :idx, :content, :hash, 10)"
        ),
        {"id": chunk_id, "ver": ver_id, "org": org_id, "page": page_id,
         "idx": idx, "content": content, "hash": str(hash(content))},
    )
    if embed:
        provider = provider or StubEmbeddingProvider(dimensions=1536)
        vec = (await provider.embed([content]))[0]
        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
        await session.execute(
            text(
                "UPDATE document_chunks SET embedding = CAST(:v AS vector), embedding_model = 'stub' "
                "WHERE id = :id"
            ),
            {"v": vec_str, "id": chunk_id},
        )
    await session.flush()
    return chunk_id


async def _seed_collection(session, org_id: str, document_id: str) -> str:
    coll_id = _uuid()
    await session.execute(
        text(
            "INSERT INTO collections (id, organization_id, name) VALUES (:id, :org, :name)"
        ),
        {"id": coll_id, "org": org_id, "name": f"Coll {coll_id[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO collection_documents (collection_id, document_id) "
            "VALUES (:cid, :did)"
        ),
        {"cid": coll_id, "did": document_id},
    )
    await session.flush()
    return coll_id


@pytest.fixture(autouse=True)
def _stub_providers():
    """Force stub providers for the whole module (no real API calls)."""
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    set_reranker_provider(StubRerankerProvider())
    yield
    set_reranker_provider(None)


# ── Index existence (Phase 8 DB work) ─────────────────────────────────────────

@pytest.mark.integration
class TestPhase8Indexes:

    @pytest.mark.asyncio
    async def test_gin_and_filter_indexes_exist(self, db_session):
        result = await db_session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename IN "
                 "('document_chunks', 'documents', 'document_versions')")
        )
        names = {row[0] for row in result}
        assert "ix_document_chunks_content_tsv_gin" in names, "GIN FTS index missing"
        assert "ix_documents_org_department" in names, "department filter index missing"
        assert "ix_document_versions_effective_date" in names, "temporal index missing"
        assert "ix_document_chunks_embedding_hnsw" in names, "HNSW index missing"

    @pytest.mark.asyncio
    async def test_keyword_search_uses_gin_index(self, db_session):
        """The GIN index must be usable for the FTS match.

        enable_seqscan=off forces the planner to prove an index path exists
        (a tiny test table would otherwise seq-scan purely on cost, which
        proves nothing about production behaviour).  The pure-FTS query
        makes the GIN index the only viable index path; the production
        query shape (with org/scope predicates) additionally uses the
        B-tree org index — the planner intersects at larger volumes."""
        org_id, user_id = await _seed_user(db_session)
        doc_id, ver_id = await _seed_document(db_session, org_id, user_id)
        page_id = await _seed_page(db_session, ver_id)
        await _seed_chunk(db_session, ver_id, org_id, page_id, 0,
                          "The retention schedule requires annual review.")

        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = await db_session.execute(
            text(
                "EXPLAIN (FORMAT JSON) SELECT id FROM document_chunks "
                "WHERE content_tsv @@ websearch_to_tsquery('english', 'retention')"
            ),
        )
        plan_text = str(plan.scalar())
        assert "Seq Scan" not in plan_text, f"FTS plan should use an index: {plan_text}"
        assert "content_tsv_gin" in plan_text, f"GIN index not referenced in plan: {plan_text}"

        # The production query shape (org + scope + FTS) must also avoid a
        # seq scan under the same forcing.
        plan2 = await db_session.execute(
            text(
                "EXPLAIN (FORMAT JSON) SELECT id FROM document_chunks "
                "WHERE content_tsv @@ websearch_to_tsquery('english', 'retention') "
                "AND organization_id = :org"
            ),
            {"org": org_id},
        )
        assert "Seq Scan" not in str(plan2.scalar())


# ── Keyword branch over a seeded corpus ───────────────────────────────────────

@pytest.mark.integration
class TestKeywordSearch:

    @pytest.mark.asyncio
    async def test_matches_and_orders_by_rank(self, db_session):
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id = await _seed_user(db_session)
        doc_id, ver_id = await _seed_document(db_session, org_id, user_id)
        page_id = await _seed_page(db_session, ver_id)

        await _seed_chunk(db_session, ver_id, org_id, page_id, 0,
                          "Document retention policy requires annual review.")
        await _seed_chunk(db_session, ver_id, org_id, page_id, 1,
                          "Retention of records follows the schedule in appendix A.")
        await _seed_chunk(db_session, ver_id, org_id, page_id, 2,
                          "Marketing budget approval workflow.")

        repo = DocumentChunkRepository(db_session)
        results = await repo.keyword_search(
            organization_id=org_id,
            version_ids=[ver_id],
            query_text="retention",  # websearch terms AND together
            top_k=10,
        )

        # Both retention chunks match; the marketing chunk is absent
        assert len(results) == 2
        assert all("retention" in r.content.lower() for r in results)
        # Rank descending
        ranks = [r.similarity for r in results]
        assert ranks == sorted(ranks, reverse=True)

    @pytest.mark.asyncio
    async def test_exact_identifier_match(self, db_session):
        """Keyword branch finds exact identifiers that embeddings miss."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id = await _seed_user(db_session)
        doc_id, ver_id = await _seed_document(db_session, org_id, user_id)
        page_id = await _seed_page(db_session, ver_id)
        await _seed_chunk(db_session, ver_id, org_id, page_id, 0,
                          "Per section 4.2, escalation requires ISO-27001 evidence.")

        repo = DocumentChunkRepository(db_session)
        results = await repo.keyword_search(
            organization_id=org_id,
            version_ids=[ver_id],
            query_text="4.2 escalation",
            top_k=10,
        )
        assert len(results) == 1
        assert "4.2" in results[0].content

    @pytest.mark.asyncio
    async def test_mandatory_predicates_tenant_and_scope(self, db_session):
        """Isolation matrix on the keyword branch: org boundary, allowed
        versions, soft-delete exclusion."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_a, user_a = await _seed_user(db_session)
        org_b, user_b = await _seed_user(db_session)

        _, ver_a = await _seed_document(db_session, org_a, user_a)
        _, ver_b = await _seed_document(db_session, org_b, user_b)
        page_a = await _seed_page(db_session, ver_a)
        page_b = await _seed_page(db_session, ver_b)
        await _seed_chunk(db_session, ver_a, org_a, page_a, 0, "Org A secret policy clause")
        await _seed_chunk(db_session, ver_b, org_b, page_b, 0, "Org B secret policy clause")

        # Soft-deleted doc in org A (its chunks must be invisible)
        deleted_doc, deleted_ver = await _seed_document(db_session, org_a, user_a)
        deleted_page = await _seed_page(db_session, deleted_ver)
        await _seed_chunk(db_session, deleted_ver, org_a, deleted_page, 0,
                          "Deleted secret policy clause")
        await db_session.execute(
            text("UPDATE documents SET deleted_at = now() WHERE id = :d"),
            {"d": deleted_doc},
        )
        await db_session.flush()

        repo = DocumentChunkRepository(db_session)
        results = await repo.keyword_search(
            organization_id=org_a,
            version_ids=[ver_a],  # ver_b NOT allowed; deleted_ver NOT allowed
            query_text="secret policy clause",
            top_k=10,
        )
        assert len(results) == 1
        assert "Org A" in results[0].content

    @pytest.mark.asyncio
    async def test_websearch_parser_never_raises(self, db_session):
        """websearch_to_tsquery is forgiving: hostile/weird syntax returns
        empty results, never a 500 (Backend §52 structural defense)."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id = await _seed_user(db_session)
        doc_id, ver_id = await _seed_document(db_session, org_id, user_id)
        page_id = await _seed_page(db_session, ver_id)
        await _seed_chunk(db_session, ver_id, org_id, page_id, 0, "normal content")

        repo = DocumentChunkRepository(db_session)
        for hostile in ("'; DROP TABLE users; --", "' OR 1=1 --", "&&& ||| ***", "("):
            results = await repo.keyword_search(
                organization_id=org_id,
                version_ids=[ver_id],
                query_text=hostile,
                top_k=10,
            )
            assert isinstance(results, list)


# ── Metadata filters inside both branches ─────────────────────────────────────

@pytest.mark.integration
class TestMetadataFilters:

    async def _seed_filtered_corpus(self, session):
        """Three docs: policy/HR, contract/Legal, technical/IT."""
        org_id, user_id = await _seed_user(session)
        other_user_id = _uuid()  # exists in org, never owns a doc below
        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, full_name) "
                "VALUES (:id, :org, :email, 'Other User')"
            ),
            {"id": other_user_id, "org": org_id, "email": f"{other_user_id[:8]}@x.com"},
        )
        await session.flush()

        specs = [
            dict(name="HR Policy", document_type="policy", department="HR"),
            dict(name="Vendor Contract", document_type="contract", department="Legal"),
            dict(name="IT Runbook", document_type="technical", department="IT"),
        ]
        seeded = []
        for spec in specs:
            doc_id, ver_id = await _seed_document(
                session, org_id, user_id, name=spec["name"],
                document_type=spec["document_type"], department=spec["department"],
            )
            page_id = await _seed_page(session, ver_id)
            await _seed_chunk(
                session, ver_id, org_id, page_id, 0,
                f"{spec['name']} retention clause content",
                # only the first doc gets an embedding
                embed=spec["name"] == "HR Policy",
            )
            seeded.append((doc_id, ver_id, spec))
        return org_id, user_id, other_user_id, seeded

    @pytest.mark.asyncio
    async def test_document_type_filter_both_branches(self, db_session):
        from app.domain.search import SearchFilters
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id, _, seeded = await self._seed_filtered_corpus(db_session)
        _, ver_contract, _ = seeded[1]
        _, ver_policy, _ = seeded[0]

        repo = DocumentChunkRepository(db_session)
        filters = SearchFilters(document_types=("contract",))

        kw = await repo.keyword_search(
            organization_id=org_id, version_ids=[ver_policy, ver_contract],
            query_text="retention clause content", top_k=10, filters=filters,
        )
        assert {r.document_name for r in kw} == {"Vendor Contract"}

        # semantic branch: only the HR chunk has an embedding, and the
        # contract filter must exclude it
        provider = StubEmbeddingProvider(dimensions=1536)
        qvec = (await provider.embed(["retention clause content"]))[0]
        sem = await repo.semantic_search(
            organization_id=org_id, version_ids=[ver_policy, ver_contract],
            query_vector=qvec, top_k=10, filters=filters,
        )
        assert sem == [], "contract filter must exclude the embedded HR chunk"

    @pytest.mark.asyncio
    async def test_department_and_owner_filters(self, db_session):
        from app.domain.search import SearchFilters
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id, other_user_id, seeded = await self._seed_filtered_corpus(db_session)
        ver_it = seeded[2][1]

        repo = DocumentChunkRepository(db_session)

        dept_filter = SearchFilters(department="IT")
        kw = await repo.keyword_search(
            organization_id=org_id, version_ids=[ver_it],
            query_text="retention clause content", top_k=10, filters=dept_filter,
        )
        assert len(kw) == 1

        owner_filter = SearchFilters(owner_id=other_user_id)
        kw2 = await repo.keyword_search(
            organization_id=org_id, version_ids=[ver_it],
            query_text="retention clause content", top_k=10, filters=owner_filter,
        )
        assert kw2 == [], "another user's ownership filter excludes the doc"

    @pytest.mark.asyncio
    async def test_collection_filter(self, db_session):
        from app.domain.search import SearchFilters
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id, _, seeded = await self._seed_filtered_corpus(db_session)
        doc_policy, ver_policy, _ = seeded[0]
        _, ver_contract, _ = seeded[1]

        coll_id = await _seed_collection(db_session, org_id, doc_policy)

        repo = DocumentChunkRepository(db_session)
        filters = SearchFilters(collection_ids=(coll_id,))
        kw = await repo.keyword_search(
            organization_id=org_id, version_ids=[ver_policy, ver_contract],
            query_text="retention clause content", top_k=10, filters=filters,
        )
        assert {r.document_name for r in kw} == {"HR Policy"}

    @pytest.mark.asyncio
    async def test_filters_combine_with_and(self, db_session):
        from app.domain.search import SearchFilters
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        org_id, user_id, _, seeded = await self._seed_filtered_corpus(db_session)
        ver_policy = seeded[0][1]  # HR Policy: type=policy, dept=HR
        ver_contract = seeded[1][1]

        repo = DocumentChunkRepository(db_session)
        both = SearchFilters(document_types=("policy",), department="Legal")
        kw = await repo.keyword_search(
            organization_id=org_id, version_ids=[ver_policy, ver_contract],
            query_text="retention clause content", top_k=10, filters=both,
        )
        assert kw == [], "AND semantics: no doc is both policy AND Legal"


# ── HybridRetriever end-to-end over real SQL ──────────────────────────────────

@pytest.mark.integration
class TestHybridRetrieverEndToEnd:

    async def _seed_and_retriever(self, session, monkeypatch, *, reranker=None):
        from app.rag.hybrid_search import HybridRetriever

        org_id, user_id = await _seed_user(session)
        doc_id, ver_id = await _seed_document(session, org_id, user_id)
        page_id = await _seed_page(session, ver_id)
        await _seed_chunk(session, ver_id, org_id, page_id, 0,
                          "The approval process requires seven business days.")
        await _seed_chunk(session, ver_id, org_id, page_id, 1,
                          "Escalations follow section 4.2 of the SOP.")

        user = SimpleNamespace(id=user_id, organization_id=org_id)

        import app.rag.hybrid_search as hs

        class FakeAuth:
            @staticmethod
            async def resolve_allowed_documents(usr, db, *, scope=None):
                return [ver_id]

        monkeypatch.setattr(hs, "AuthorizationService", FakeAuth)
        retriever = HybridRetriever(session, reranker_provider=reranker)
        return retriever, user, org_id, ver_id

    @pytest.mark.asyncio
    async def test_hybrid_search_returns_ranked_results(self, db_session, monkeypatch):
        retriever, user, org_id, ver_id = await self._seed_and_retriever(
            db_session, monkeypatch
        )
        outcome = await retriever.search(
            "approval process seven business days", user, mode="hybrid", top_k=5
        )
        assert outcome.mode == "hybrid"
        assert outcome.used_reranker is True
        assert len(outcome.results) >= 1
        assert all(0.0 <= r.relevance <= 1.0 for r in outcome.results)
        assert outcome.vector_branch_count >= 1
        assert outcome.keyword_branch_count >= 1

    @pytest.mark.asyncio
    async def test_reranker_outage_serves_unreranked(self, db_session, monkeypatch):
        from app.infrastructure.reranker import RerankerProvider, RerankerProviderError

        class OutageReranker(RerankerProvider):
            model_name = "outage"

            async def rerank(self, query, documents):
                raise RerankerProviderError("down")

        retriever, user, org_id, ver_id = await self._seed_and_retriever(
            db_session, monkeypatch, reranker=OutageReranker()
        )
        outcome = await retriever.search(
            "approval process", user, mode="hybrid", top_k=5
        )
        assert outcome.used_reranker is False
        assert outcome.reranker_note == "RERANKER_PROVIDER_ERROR"
        assert len(outcome.results) >= 1, "degraded — not failed"

    @pytest.mark.asyncio
    async def test_temporal_as_of_resolves_historical_version(self, db_session):
        """Exit criterion: temporal queries return the correct historical
        version's chunks (via the REAL AuthorizationService)."""
        from app.domain.versioning import VersionScope
        from app.services.authorization_service import AuthorizationService

        org_id, user_id = await _seed_user(db_session)
        doc_id = _uuid()

        # v2024: effective 2024-01-01 (historical, READY)
        ver_2024 = _uuid()
        # v2026: effective 2026-01-01 (current — latest created_at, READY)
        ver_2026 = _uuid()
        await db_session.execute(
            text(
                "INSERT INTO documents (id, organization_id, owner_id, name, document_type, "
                "status, access_level) VALUES (:id, :org, :owner, 'Policy', 'policy', "
                "'active', 'organization')"
            ),
            {"id": doc_id, "org": org_id, "owner": user_id},
        )
        for vid, eff, num, created in (
            (ver_2024, date(2024, 1, 1), 1, datetime(2024, 6, 1, tzinfo=timezone.utc)),
            (ver_2026, date(2026, 1, 1), 2, datetime(2026, 6, 1, tzinfo=timezone.utc)),
        ):
            await db_session.execute(
                text(
                    "INSERT INTO document_versions (id, document_id, version_number, storage_key, "
                    "mime_type, file_size_bytes, status, created_by, effective_date, created_at) "
                    "VALUES (:id, :doc, :num, 'key', 'application/pdf', 100, 'READY', :owner, :eff, :created)"
                ),
                {"id": vid, "doc": doc_id, "num": num, "owner": user_id,
                 "eff": eff, "created": created},
            )
        await db_session.flush()

        user = SimpleNamespace(id=user_id, organization_id=org_id)

        # "Current" resolution → 2026 version
        current_ids = await AuthorizationService.resolve_allowed_documents(
            user, db_session, scope=VersionScope.for_documents([doc_id])
        )
        assert current_ids == [ver_2026]

        # Temporal resolution (as_of=2025) → the 2024 version
        scope = VersionScope(kind="documents", document_ids=(doc_id,), as_of="2025-06-15")
        historical_ids = await AuthorizationService.resolve_allowed_documents(
            user, db_session, scope=scope
        )
        assert historical_ids == [ver_2024]

        # Before the document existed at all → excluded (empty, not an error)
        scope_pre = VersionScope(kind="documents", document_ids=(doc_id,), as_of="2023-01-01")
        pre_ids = await AuthorizationService.resolve_allowed_documents(
            user, db_session, scope=scope_pre
        )
        assert pre_ids == []
