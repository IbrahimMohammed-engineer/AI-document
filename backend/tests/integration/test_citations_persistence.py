"""
Integration tests — citations + messages persistence (Phase 10).

Requires testcontainers (real Postgres with migrations applied through 010).

Tests (roadmap Phase 10 §Testing):
  - Atomicity: the assistant message + its citations write in ONE
    transaction (Backend §50) — an injected failure mid-write leaves
    NEITHER row behind.
  - RESTRICT FK policy: a cited chunk/page/version/document cannot be
    deleted while a citation references it.
  - Cascade: deleting the message removes its citations (the only
    permitted cascade on the citation table).
  - Round-trip: persisted rows read back with the exact denormalized
    fields that were resolved at generation time.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.repositories.message_repository import MessageRepository
from app.rag.citations import QuotedSpan, ResolvedCitation


# ── Seed helpers (raw SQL — same pattern as the Phase 8/9 API tests) ──────────

def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_document_graph(session_factory, org_id: str, owner_id: str) -> dict:
    """document → version → page → chunk rows; returns all ids."""
    ids = {
        "document_id": _uuid(),
        "version_id": _uuid(),
        "page_id": _uuid(),
        "chunk_id": _uuid(),
    }
    async with session_factory() as session:
        await session.execute(text(
            "INSERT INTO documents (id, organization_id, owner_id, name, "
            "document_type, status, access_level) "
            "VALUES (:id, :org, :owner, 'Citable Policy', 'policy', "
            "'active', 'organization')"
        ), {"id": ids["document_id"], "org": org_id, "owner": owner_id})
        await session.execute(text(
            "INSERT INTO document_versions (id, document_id, version_number, "
            "storage_key, mime_type, file_size_bytes, status, created_by, "
            "effective_date) VALUES (:id, :doc, 1, 'key', 'application/pdf', "
            "100, 'READY', :owner, DATE '2026-01-01')"
        ), {"id": ids["version_id"], "doc": ids["document_id"], "owner": owner_id})
        await session.execute(text(
            "INSERT INTO document_pages (id, document_version_id, page_number, text) "
            "VALUES (:id, :ver, 4, 'page content')"
        ), {"id": ids["page_id"], "ver": ids["version_id"]})
        await session.execute(text(
            "INSERT INTO document_chunks (id, document_version_id, "
            "organization_id, page_id, chunk_index, content, content_hash, "
            "token_count) VALUES (:id, :ver, :org, :page, 0, "
            "'The approval process requires four stages.', :hash, 12)"
        ), {"id": ids["chunk_id"], "ver": ids["version_id"], "org": org_id,
            "page": ids["page_id"], "hash": _uuid()})
        await session.commit()
    return ids


async def _create_org_and_user(session_factory) -> tuple[str, str]:
    """Minimal org + admin user rows (mirrors the seeded RBAC catalogs)."""
    org_id, user_id = _uuid(), _uuid()
    async with session_factory() as session:
        await session.execute(text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, 'Cite Org', :slug)"
        ), {"id": org_id, "slug": f"cite-{uuid.uuid4().hex[:8]}"})
        await session.execute(text(
            "INSERT INTO users (id, organization_id, email, password_hash, "
            "full_name, is_active) VALUES (:id, :org, :email, 'x', 'Tester', true)"
        ), {"id": user_id, "org": org_id,
            "email": f"{uuid.uuid4().hex[:8]}@cite.test"})
        await session.commit()
    return org_id, user_id


def _resolved_citation(index: int, ids: dict) -> ResolvedCitation:
    content = "The approval process requires four stages."
    return ResolvedCitation(
        index=index,
        chunk_id=ids["chunk_id"],
        document_id=ids["document_id"],
        document_version_id=ids["version_id"],
        document_name="Citable Policy",
        page_id=ids["page_id"],
        page_number=4,
        section="4.2 Regulatory Review",
        relevance=0.87,
        quoted=QuotedSpan(
            text=content, char_start=0, char_end=len(content)
        ),
        claim_text="The approval process requires four stages.",
    )


# ── The happy path + round-trip ───────────────────────────────────────────────

@pytest.mark.integration
class TestAtomicPersistence:

    async def test_message_and_citations_persist_together(
        self, app_session_factory
    ):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        ids = await _seed_document_graph(app_session_factory, org_id, user_id)

        async with app_session_factory() as session:
            repo = MessageRepository(session)
            message = await repo.save_assistant_message_with_citations(
                content="The approval process requires four stages. [1]",
                groundedness="grounded",
                citations=[_resolved_citation(1, ids)],
                model="stub-llm",
                prompt_tokens=120,
                completion_tokens=9,
                retrieval_ms=42,
                latency_ms=200,
            )
            assert message.id is not None

        async with app_session_factory() as session:
            row = (await session.execute(text(
                "SELECT role, content, model, prompt_tokens, groundedness, "
                "retrieval_ms, latency_ms FROM messages WHERE id = :id"
            ), {"id": message.id})).mappings().one()
            assert row["role"] == "ASSISTANT"
            assert row["groundedness"] == "grounded"
            assert row["model"] == "stub-llm"
            assert row["retrieval_ms"] == 42

            cit = (await session.execute(text(
                "SELECT citation_index, document_id, document_version_id, "
                "chunk_id, page_id, page_number, section, quoted_text, "
                "char_start, char_end, relevance_score "
                "FROM citations WHERE message_id = :id"
            ), {"id": message.id})).mappings().all()
            assert len(cit) == 1
            c = cit[0]
            assert c["citation_index"] == 1
            assert str(c["document_id"]) == ids["document_id"]
            assert str(c["chunk_id"]) == ids["chunk_id"]
            assert str(c["page_id"]) == ids["page_id"]
            assert c["page_number"] == 4
            assert c["section"] == "4.2 Regulatory Review"
            assert "four stages" in c["quoted_text"]
            assert c["char_start"] == 0
            assert float(c["relevance_score"]) == pytest.approx(0.87)

    async def test_injected_failure_leaves_neither_row(
        self, app_session_factory
    ):
        """Atomicity (Backend §50): a citation FK violation rolls back the
        message row too — a message with orphaned markers is impossible."""
        org_id, user_id = await _create_org_and_user(app_session_factory)
        ids = await _seed_document_graph(app_session_factory, org_id, user_id)

        bad_citation = _resolved_citation(1, ids)
        bad_citation.document_id = _uuid()  # non-existent document → FK fail

        async with app_session_factory() as session:
            repo = MessageRepository(session)
            with pytest.raises(IntegrityError):
                await repo.save_assistant_message_with_citations(
                    content="Answer with a marker. [1]",
                    groundedness="grounded",
                    citations=[bad_citation],
                )
                await session.rollback()

        # Scoped to THIS test's data — other tests' rows may legitimately exist.
        async with app_session_factory() as session:
            count = (await session.execute(text(
                "SELECT COUNT(*) FROM messages WHERE content = 'Answer with a marker. [1]'"
            ))).scalar_one()
            assert count == 0, "message row must NOT exist when the citation write fails"
            cit_count = (await session.execute(text(
                "SELECT COUNT(*) FROM citations c JOIN messages m "
                "ON m.id = c.message_id WHERE m.content = 'Answer with a marker. [1]'"
            ))).scalar_one()
            assert cit_count == 0


# ── FK policy ─────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestRestrictForeignKeys:

    async def test_cited_chunk_cannot_be_deleted(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        ids = await _seed_document_graph(app_session_factory, org_id, user_id)

        async with app_session_factory() as session:
            await MessageRepository(session).save_assistant_message_with_citations(
                content="Cited. [1]",
                groundedness="grounded",
                citations=[_resolved_citation(1, ids)],
            )

        async with app_session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(text(
                    "DELETE FROM document_chunks WHERE id = :id"
                ), {"id": ids["chunk_id"]})
                await session.rollback()

    async def test_cited_page_cannot_be_deleted(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        ids = await _seed_document_graph(app_session_factory, org_id, user_id)

        async with app_session_factory() as session:
            await MessageRepository(session).save_assistant_message_with_citations(
                content="Cited. [1]",
                groundedness="grounded",
                citations=[_resolved_citation(1, ids)],
            )

        async with app_session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(text(
                    "DELETE FROM document_pages WHERE id = :id"
                ), {"id": ids["page_id"]})
                await session.rollback()

    async def test_deleting_message_cascades_citations(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        ids = await _seed_document_graph(app_session_factory, org_id, user_id)

        async with app_session_factory() as session:
            message = await MessageRepository(
                session
            ).save_assistant_message_with_citations(
                content="Cited. [1]",
                groundedness="grounded",
                citations=[_resolved_citation(1, ids)],
            )

        async with app_session_factory() as session:
            await session.execute(text(
                "DELETE FROM messages WHERE id = :id"
            ), {"id": message.id})
            await session.commit()
            # Scoped to the deleted message — other tests' citations may exist.
            remaining = (await session.execute(text(
                "SELECT COUNT(*) FROM citations WHERE message_id = :id"
            ), {"id": message.id})).scalar_one()
            assert remaining == 0
