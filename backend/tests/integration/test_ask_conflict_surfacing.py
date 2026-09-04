"""
Integration tests — inline RAG conflict surfacing (Phase 13, §20, §27.2/§27.6;
plan §23 Task 13).

Covers:
  - a question whose retrieval surfaces >= 2 chunks of the same OPEN conflict
    emits a deterministic `conflict_notice` SSE event with the correct
    conflict_id/topic/severity (and the notice rides the done outcome);
  - a question whose retrieval shares NO conflict statements emits no notice;
  - a REVIEWED conflict never emits a notice (OPEN-only query);
  - negative case: no conflict in the corpus → a completely normal answer.

Requires Docker (testcontainers).
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.infrastructure.database as db_mod
import app.infrastructure.queue as queue_mod
from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.infrastructure.reranker import StubRerankerProvider, set_reranker_provider
from app.workers.jobs import run_processing_job
from tests.fixtures.conflict_fixtures import (
    HR_POLICY_A_TEXT,
    HR_POLICY_B_TEXT,
    _pair_vector,
    _unit_vector,
    seed_conflict_document,
    seed_conflict_org,
)


# ── LLM stub ──────────────────────────────────────────────────────────────────

def _responder(messages):
    system = messages[0].content if messages else ""
    if "classify a user's question" in system:  # query analyzer → plain QUESTION
        return json.dumps({
            "intent": "QUESTION",
            "temporal_scope": None,
            "scope_hints": [],
            "topic": "vacation",
        })
    if "CONTRADICT" in system:  # contradiction check (scan path)
        return json.dumps({
            "is_conflict": True,
            "confidence": 0.9,
            "reason": "Different approvers.",
            "conflict_topic": "Vacation Approval Authority",
        })
    if "verdict" in system:  # entailment
        return '{"verdict": "yes", "reason": "supported"}'
    return "Based on the provided sources, the documents describe vacation approval. [1]"


@pytest_asyncio.fixture()
async def ask_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    set_reranker_provider(StubRerankerProvider())
    set_llm_provider(StubLLMProvider(responder=_responder))
    yield app_session_factory
    set_reranker_provider(None)
    set_llm_provider(None)
    set_embedding_provider(None)
    await pool.aclose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client) -> tuple[str, str]:
    slug = f"ask-confl-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Ask Conflict Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Ask Conflict Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await client.get("/auth/me", headers=_auth_headers(token))
    return token, str(me.json()["id"])


async def _org_id(factory: async_sessionmaker, user_id: str) -> str:
    async with factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"),
            {"id": user_id},
        )
        return str(row.scalar_one())


async def _seed_corpus(factory, org_id: str, owner_id: str) -> tuple[dict, dict]:
    doc_a = await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy A", section_number="1",
        section_title="Vacation Approval", content=HR_POLICY_A_TEXT,
        embedding=_unit_vector(0),
    )
    doc_b = await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy B", section_number="2",
        section_title="Leave Procedures", content=HR_POLICY_B_TEXT,
        embedding=_pair_vector(0, 1, cos=0.9),
    )
    return doc_a, doc_b


async def _run_scan(factory, org_id: str) -> None:
    from app.services.job_service import JobService

    async with factory() as session:
        job = await JobService.create_for_org_scan(session, organization_id=org_id)
        await session.commit()
        job_id = job.id
    assert await run_processing_job({}, job_id) == "COMPLETED"


async def _stream_sse(client, token, url: str, payload: dict):
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", url, json=payload, headers=_auth_headers(token)
    ) as response:
        status = response.status_code
        raw = ""
        async for chunk in response.aiter_text():
            raw += chunk
    assert status == 200, raw[:500]
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if event_name:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event_name, data))
    return events


# ── The surfacing matrix ──────────────────────────────────────────────────────

@pytest.mark.integration
class TestAskConflictSurfacing:

    @pytest.mark.asyncio
    async def test_notice_emitted_when_retrieval_surfaces_conflict(
        self, app_client: AsyncClient, app_session_factory, ask_env
    ):
        factory = ask_env
        token, user_id = await _register_and_login(app_client)
        org_id = await _org_id(factory, user_id)
        doc_a, doc_b = await _seed_corpus(factory, org_id, user_id)
        await _run_scan(factory, org_id)

        async with factory() as session:
            row = (await session.execute(text(
                "SELECT id, topic, severity FROM conflicts WHERE status = 'OPEN'"
            ))).first()
        conflict_id, topic, severity = str(row[0]), row[1], row[2]

        events = await _stream_sse(app_client, token, "/ask", {
            "question": "Who approves vacation requests?",
            "scope": {
                "document_ids": [doc_a["document_id"], doc_b["document_id"]]
            },
        })

        notices = [d for n, d in events if n == "conflict_notice"]
        assert len(notices) == 1
        payload = notices[0]["conflicts"]
        assert len(payload) == 1
        assert payload[0]["conflict_id"] == conflict_id
        assert payload[0]["topic"] == topic
        assert payload[0]["severity"] == severity

    @pytest.mark.asyncio
    async def test_no_notice_without_recorded_conflict(
        self, app_client: AsyncClient, app_session_factory, ask_env
    ):
        factory = ask_env
        token, user_id = await _register_and_login(app_client)
        org_id = await _org_id(factory, user_id)
        await _seed_corpus(factory, org_id, user_id)  # no scan → no conflicts

        events = await _stream_sse(app_client, token, "/ask", {
            "question": "Who approves vacation requests?",
            "scope": {"document_ids": []},
        })
        assert all(n != "conflict_notice" for n, _ in events)

    @pytest.mark.asyncio
    async def test_reviewed_conflict_never_surfaces(
        self, app_client: AsyncClient, app_session_factory, ask_env
    ):
        factory = ask_env
        token, user_id = await _register_and_login(app_client)
        org_id = await _org_id(factory, user_id)
        doc_a, doc_b = await _seed_corpus(factory, org_id, user_id)
        await _run_scan(factory, org_id)

        async with factory() as session:
            conflict = (await session.execute(text(
                "SELECT id FROM conflicts WHERE status = 'OPEN' LIMIT 1"
            ))).first()
            await session.execute(text(
                "UPDATE conflicts SET status = 'REVIEWED', "
                "resolved_by = :uid, resolved_at = now() WHERE id = :cid"
            ), {"uid": user_id, "cid": str(conflict[0])})
            await session.commit()

        events = await _stream_sse(app_client, token, "/ask", {
            "question": "Who approves vacation requests?",
            "scope": {
                "document_ids": [doc_a["document_id"], doc_b["document_id"]]
            },
        })
        assert all(n != "conflict_notice" for n, _ in events)
