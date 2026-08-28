"""
Integration tests for the Phase 7 embedding pipeline.

Requires testcontainers (PostgreSQL+pgvector) — tests are skipped when
Docker is unavailable.

Tests:
  - Embed-and-persist round-trip with StubEmbeddingProvider
  - Resume after crash: only un-embedded chunks are processed
  - Tenant isolation: org A's chunks never appear in org B's search
  - HNSW recall sanity: seeded similar/dissimilar vectors
  - count_embedded_for_version tracks progress correctly

All tests use the StubEmbeddingProvider — no real API calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider

# The Document ORM relationships reference "Organization"/"User" by class
# name — those mappers must be registered before any ORM operation.
import app.models.organization  # noqa: F401
import app.models.user  # noqa: F401


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_org_and_doc(session) -> tuple[str, str, str, str]:
    """Create minimal org + user + document + version + page rows.

    Returns (org_id, doc_id, ver_id, page_id).
    """
    from sqlalchemy import text

    org_id = _uuid()
    user_id = _uuid()
    doc_id = _uuid()
    ver_id = _uuid()
    page_id = _uuid()

    await session.execute(text("INSERT INTO organizations (id, name, slug, plan) VALUES (:id, :n, :s, 'basic')"),
                          {"id": org_id, "n": f"Org {org_id[:8]}", "s": org_id[:8]})
    await session.execute(text(
        "INSERT INTO users (id, organization_id, email, full_name) "
        "VALUES (:id, :org, :email, 'Test User')"
    ), {"id": user_id, "org": org_id, "email": f"{user_id[:8]}@example.com"})
    await session.execute(text(
        "INSERT INTO documents (id, organization_id, owner_id, name, document_type, status, access_level) "
        "VALUES (:id, :org, :owner, 'Test Doc', 'technical', 'active', 'organization')"
    ), {"id": doc_id, "org": org_id, "owner": user_id})
    await session.execute(text(
        "INSERT INTO document_versions (id, document_id, version_number, storage_key, mime_type, "
        "file_size_bytes, status, created_by) "
        "VALUES (:id, :doc, 1, 'key', 'application/pdf', 100, 'CHUNKING', :owner)"
    ), {"id": ver_id, "doc": doc_id, "owner": user_id})
    await session.execute(text(
        "INSERT INTO document_pages (id, document_version_id, page_number, text) "
        "VALUES (:id, :ver, 1, 'page content')"
    ), {"id": page_id, "ver": ver_id})
    await session.flush()
    return org_id, doc_id, ver_id, page_id


async def _insert_chunk(session, ver_id: str, org_id: str, page_id: str, idx: int, content: str) -> str:
    """Insert a single chunk row (no embedding)."""
    from sqlalchemy import text
    chunk_id = _uuid()
    content_hash = str(hash(content))
    await session.execute(text(
        "INSERT INTO document_chunks "
        "(id, document_version_id, organization_id, page_id, chunk_index, content, content_hash, token_count) "
        "VALUES (:id, :ver, :org, :page, :idx, :content, :hash, 10)"
    ), {"id": chunk_id, "ver": ver_id, "org": org_id, "page": page_id,
        "idx": idx, "content": content, "hash": content_hash})
    await session.flush()
    return chunk_id


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestEmbeddingPipeline:

    @pytest.mark.asyncio
    async def test_embed_and_persist_round_trip(self, db_session):
        """Basic: embed N chunks, verify all have non-null embeddings."""
        from app.ingestion.embedding_stage import run_embedding
        from app.repositories.document_chunk_repository import DocumentChunkRepository
        from app.repositories.document_repository import DocumentVersionRepository
        from app.repositories.processing_job_repository import ProcessingJobRepository

        org_id, doc_id, ver_id, page_id = await _seed_org_and_doc(db_session)

        # Insert 5 chunks
        for i in range(5):
            await _insert_chunk(db_session, ver_id, org_id, page_id, i, f"chunk text {i}")

        # Build stub provider
        provider = StubEmbeddingProvider(dimensions=1536)
        set_embedding_provider(provider)

        # We need minimal job + doc + version objects
        from unittest.mock import MagicMock
        job = MagicMock()
        job.organization_id = org_id
        version = MagicMock()
        version.id = ver_id
        version.status = "CHUNKING"
        version.document_id = doc_id
        document = MagicMock()
        document.organization_id = org_id
        document.id = doc_id

        job_repo = MagicMock()
        job_repo.mark_progress = AsyncMock()
        ver_repo = MagicMock()
        ver_repo.update_status = AsyncMock()

        await run_embedding(
            db_session,
            version=version,
            document=document,
            job=job,
            job_repo=job_repo,
            ver_repo=ver_repo,
            provider=provider,
        )

        # Verify all 5 chunks now have embeddings
        chunk_repo = DocumentChunkRepository(db_session)
        embedded = await chunk_repo.count_embedded_for_version(ver_id)
        total = await chunk_repo.count_for_version(ver_id)
        assert embedded == 5
        assert total == 5

    @pytest.mark.asyncio
    async def test_resume_skips_already_embedded(self, db_session):
        """Crash-resume: pre-embedded chunks are not re-embedded."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository
        from sqlalchemy import text

        org_id, doc_id, ver_id, page_id = await _seed_org_and_doc(db_session)

        # Insert 4 chunks
        chunk_ids = []
        for i in range(4):
            cid = await _insert_chunk(db_session, ver_id, org_id, page_id, i, f"text {i}")
            chunk_ids.append(cid)

        # Pre-embed chunks 0 and 1 (simulate previous run)
        provider = StubEmbeddingProvider(dimensions=1536)
        pre_vectors = await provider.embed(["text 0", "text 1"])
        for i, vec in enumerate(pre_vectors):
            vec_str = "[" + ",".join(str(x) for x in vec) + "]"
            await db_session.execute(
                text("UPDATE document_chunks SET embedding = CAST(:v AS vector), embedding_model = 'stub' WHERE id = :id"),
                {"v": vec_str, "id": chunk_ids[i]},
            )
        await db_session.commit()

        chunk_repo = DocumentChunkRepository(db_session)
        # Verify only 2 are embedded at this point
        assert await chunk_repo.count_embedded_for_version(ver_id) == 2

        # Track embed calls
        embed_call_count = 0
        original_embed = provider.embed

        async def tracked_embed(texts):
            nonlocal embed_call_count
            embed_call_count += 1
            return await original_embed(texts)

        provider.embed = tracked_embed

        from unittest.mock import MagicMock, AsyncMock
        version = MagicMock()
        version.id = ver_id
        version.status = "CHUNKING"
        version.document_id = doc_id
        document = MagicMock()
        document.organization_id = org_id
        document.id = doc_id
        job = MagicMock()
        job.organization_id = org_id
        job_repo = MagicMock()
        job_repo.mark_progress = AsyncMock()
        ver_repo = MagicMock()
        ver_repo.update_status = AsyncMock()

        from app.ingestion.embedding_stage import run_embedding
        await run_embedding(
            db_session,
            version=version,
            document=document,
            job=job,
            job_repo=job_repo,
            ver_repo=ver_repo,
            provider=provider,
        )

        # All 4 should now be embedded
        assert await chunk_repo.count_embedded_for_version(ver_id) == 4
        # Provider was called only for the 2 remaining chunks (1 batch of 2)
        assert embed_call_count == 1

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_search(self, db_session):
        """Org A's semantic_search never returns Org B's chunks."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository
        from sqlalchemy import text

        # Seed two orgs
        org_a_id, doc_a_id, ver_a_id, page_a_id = await _seed_org_and_doc(db_session)
        org_b_id, doc_b_id, ver_b_id, page_b_id = await _seed_org_and_doc(db_session)

        provider = StubEmbeddingProvider(dimensions=1536)

        # Embed and store a chunk for each org
        for org_id, ver_id, page_id, content in [
            (org_a_id, ver_a_id, page_a_id, "Org A secret policy"),
            (org_b_id, ver_b_id, page_b_id, "Org B secret policy"),
        ]:
            cid = await _insert_chunk(db_session, ver_id, org_id, page_id, 0, content)
            vec = (await provider.embed([content]))[0]
            vec_str = "[" + ",".join(str(x) for x in vec) + "]"
            await db_session.execute(
                text("UPDATE document_chunks SET embedding = CAST(:v AS vector), embedding_model = 'stub' WHERE id = :id"),
                {"v": vec_str, "id": cid},
            )
        await db_session.commit()

        # Make version statuses READY (needed for the search JOIN)
        await db_session.execute(
            text("UPDATE document_versions SET status = 'READY' WHERE id IN (:a, :b)"),
            {"a": ver_a_id, "b": ver_b_id},
        )
        await db_session.commit()

        # Search as Org A — should never see Org B's chunk
        query_vector = (await provider.embed(["secret policy"]))[0]
        chunk_repo = DocumentChunkRepository(db_session)

        results_a = await chunk_repo.semantic_search(
            organization_id=org_a_id,
            version_ids=[ver_a_id],
            query_vector=query_vector,
            top_k=10,
        )
        doc_ids_in_results = {r.document_id for r in results_a}
        assert doc_b_id not in doc_ids_in_results

    @pytest.mark.asyncio
    async def test_semantic_search_empty_version_ids_raises(self, db_session):
        """semantic_search with empty version_ids must raise ValueError."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository

        chunk_repo = DocumentChunkRepository(db_session)
        with pytest.raises(ValueError, match="empty version_ids"):
            await chunk_repo.semantic_search(
                organization_id=_uuid(),
                version_ids=[],
                query_vector=[0.0] * 1536,
            )

    @pytest.mark.asyncio
    async def test_count_embedded_tracks_progress(self, db_session):
        """count_embedded_for_version counts correctly as embeddings are written."""
        from app.repositories.document_chunk_repository import DocumentChunkRepository
        from sqlalchemy import text

        org_id, doc_id, ver_id, page_id = await _seed_org_and_doc(db_session)
        chunk_repo = DocumentChunkRepository(db_session)

        # Initially zero
        assert await chunk_repo.count_embedded_for_version(ver_id) == 0

        provider = StubEmbeddingProvider(dimensions=1536)
        cid = await _insert_chunk(db_session, ver_id, org_id, page_id, 0, "hello world")

        # Still zero (embedding is NULL)
        assert await chunk_repo.count_embedded_for_version(ver_id) == 0

        # Write embedding
        vec = (await provider.embed(["hello world"]))[0]
        await chunk_repo.update_embeddings_batch_raw([
            {"chunk_id": cid, "embedding": vec, "embedding_model": "stub"}
        ])
        await db_session.commit()

        assert await chunk_repo.count_embedded_for_version(ver_id) == 1
