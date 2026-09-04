"""
Integration tests — CONFLICT_DETECTION chat-intent routing (Phase 13, §19,
§27.2; plan §23 Task 12, mirroring test_chat_change_detection.py).

The LLM stub discriminates calls by system prompt so one provider serves
the whole conversation turn (analyzer → CONFLICT_DETECTION JSON;
contradiction check → confirmed conflict; narration → prose).

Covers:
  - a persisted, authorized OPEN conflict answers with a narrated,
    citation-backed message persisted as a normal ASSISTANT Message;
  - zero accessible conflicts → the deterministic "none found" response
    (no LLM narration call for that case);
  - a conflict backed by an inaccessible document is never mentioned.

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
from app.infrastructure.llm import StubLLMProvider, set_llm_provider
from app.workers.jobs import run_processing_job
from tests.fixtures.conflict_fixtures import (
    install_conflict_llm_stub,
    seed_conflict_corpus,
    set_chunk_embedding,
    _pair_vector,
    _unit_vector,
)
from tests.fixtures.conflict_fixtures import seed_conflict_document, seed_conflict_org


# ── LLM stub (CONFLICT_DETECTION always; QUESTION for the negative path) ─────

def _responder(messages):
    system = messages[0].content if messages else ""
    if "classify a user's question" in system:  # query analyzer
        return json.dumps({
            "intent": "CONFLICT_DETECTION",
            "temporal_scope": None,
            "scope_hints": [],
            "topic": "conflicts",
        })
    if "CONTRADICT" in system:  # contradiction check
        return json.dumps({
            "is_conflict": True,
            "confidence": 0.9,
            "reason": "Different approvers.",
            "conflict_topic": "Vacation Approval Authority",
        })
    if "narrate a list of already-detected document conflicts" in system:
        return "HR Policy A and HR Policy B disagree about vacation approval."
    if "verdict" in system:  # entailment (unused on this path)
        return '{"verdict": "yes", "reason": "supported"}'
    return "standalone query"


@pytest_asyncio.fixture()
async def chat_conflict_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    set_llm_provider(StubLLMProvider(responder=_responder))
    yield app_session_factory
    set_llm_provider(None)
    await pool.aclose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client) -> str:
    slug = f"chat-confl-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Chat Conflict Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Chat Conflict Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _seed_conflict(factory, org_id: str, owner_id: str) -> str:
    """Seed the two-document corpus and run a scan, creating one conflict."""
    from tests.fixtures.conflict_fixtures import (
        HR_POLICY_A_TEXT,
        HR_POLICY_B_TEXT,
    )
    from tests.fixtures.conflict_fixtures import _pair_vector, _unit_vector

    doc_a = await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy A", section_number="1",
        section_title="Vacation Approval", content=HR_POLICY_A_TEXT,
        embedding=_unit_vector(0), effective_date=None,
    )
    doc_b = await seed_conflict_document(
        factory, organization_id=org_id, owner_id=owner_id,
        name="HR Policy B", section_number="2",
        section_title="Leave Procedures", content=HR_POLICY_B_TEXT,
        embedding=_pair_vector(0, 1, cos=0.9), effective_date=None,
    )
    from app.services.job_service import JobService

    async with factory() as session:
        job = await JobService.create_for_org_scan(session, organization_id=org_id)
        await session.commit()
        job_id = job.id
    assert await run_processing_job({}, job_id) == "COMPLETED"
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT id FROM conflicts WHERE status = 'OPEN' LIMIT 1"
        ))).first()
    assert row is not None
    return str(row[0])


async def _stream_sse(client, token, method: str, url: str, payload: dict):
    events: list[tuple[str, dict]] = []
    async with client.stream(
        method, url, json=payload, headers=_auth_headers(token)
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


def _done_answer(events) -> str:
    for name, data in reversed(events):
        if name == "done":
            return data.get("answer") or ""
    return ""


# ── The routing paths ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestChatConflictDetection:

    @pytest.mark.asyncio
    async def test_conflict_question_returns_narrated_cited_answer(
        self, app_client: AsyncClient, app_session_factory, chat_conflict_env
    ):
        factory = chat_conflict_env
        token = await _register_and_login(app_client)
        async with factory() as session:
            from app.models.user import User

            user = (await session.execute(text(
                "SELECT id, organization_id FROM users LIMIT 1"
            ))).first()

        conflict_id = await _seed_conflict(factory, str(user[1]), str(user[0]))

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "Are there conflicting rules about vacation?",
                    "scope": {"type": "knowledge_base", "document_ids": []},
                }
            },
        )
        answer = _done_answer(events)
        assert "disagree" in answer.lower()

        # Citations ride the stream and the done payload
        citation_events = [e for e in events if e[0] == "citation"]
        assert len(citation_events) >= 2  # both statements referenced
        done_payload = next(d for n, d in events if n == "done")
        assert done_payload["citations"]

        # The narrated answer PERSISTED as a normal ASSISTANT message
        async with factory() as session:
            row = await session.execute(text(
                "SELECT m.content, count(c.id) FROM messages m "
                "LEFT JOIN citations c ON c.message_id = m.id "
                "WHERE m.conversation_id = :cid AND m.role = 'ASSISTANT' "
                "GROUP BY m.content, m.created_at ORDER BY m.created_at DESC LIMIT 1"
            ), {"cid": events[0][1]["conversation_id"]})
            content, citation_rows = row.first()
        assert "disagree" in content.lower()
        assert citation_rows >= 1

    @pytest.mark.asyncio
    async def test_zero_conflicts_gets_deterministic_none_found(
        self, app_client: AsyncClient, app_session_factory, chat_conflict_env
    ):
        factory = chat_conflict_env
        token = await _register_and_login(app_client)

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "Are there conflicting rules about vacation?",
                    "scope": {"type": "knowledge_base", "document_ids": []},
                }
            },
        )
        answer = _done_answer(events)
        assert "didn't find any recorded conflicts" in answer

    @pytest.mark.asyncio
    async def test_conflict_backed_by_inaccessible_document_not_mentioned(
        self, app_client: AsyncClient, app_session_factory, chat_conflict_env
    ):
        factory = chat_conflict_env
        token = await _register_and_login(app_client)
        async with factory() as session:
            user = (await session.execute(text(
                "SELECT id, organization_id FROM users LIMIT 1"
            ))).first()
        org_id, owner_id = str(user[1]), str(user[0])

        # The corpus conflict spans doc A + doc B; scope the conversation to
        # ONLY doc A — the conflict references the inaccessible doc B, so it
        # must never reach the answer.
        from tests.fixtures.conflict_fixtures import (
            HR_POLICY_A_TEXT,
            HR_POLICY_B_TEXT,
        )

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
        from app.services.job_service import JobService

        async with factory() as session:
            job = await JobService.create_for_org_scan(
                session, organization_id=org_id
            )
            await session.commit()
            job_id = job.id
        await run_processing_job({}, job_id)

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "Are there conflicting rules about vacation?",
                    "scope": {
                        "type": "selected_documents",
                        "document_ids": [doc_a["document_id"]],
                    },
                }
            },
        )
        answer = _done_answer(events)
        # doc B's conflict is filtered out — deterministic none-found answer
        assert "didn't find any recorded conflicts" in answer
        assert "Policy B" not in answer
