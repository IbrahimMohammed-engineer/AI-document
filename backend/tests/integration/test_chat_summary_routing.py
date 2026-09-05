"""
Integration tests — SUMMARY / EXTRACTION chat-intent routing (Phase 14,
plan §8.3/§15 exit criterion 2/5; mirrors test_chat_change_detection.py).

The LLM stub discriminates calls by system prompt so one provider serves
the whole conversation turn:
  - analyzer        → SUMMARY (or EXTRACTION) intent JSON
  - summary builder → constrained JSON draft citing SOURCE 1..3
  - entailment      → yes

Covers (roadmap exit criteria 2 + 5):
  - "Summarize this policy" in chat routes to SummaryService: with exactly
    one document in scope → get_or_create → acknowledgement turn (stream
    never blocks) → after the worker runs, re-asking returns the narrated,
    citation-backed PERSISTED summary (normal ASSISTANT message + Citations)
  - identical SSE contract: start → [citation*] → done (structured like
    every other intent's stream)
  - zero documents in scope → clarifying message, never a fabricated answer
  - extraction: run-from-chat creates once, then reuses the COMPLETED run

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
from app.workers.jobs import run_processing_job
from tests.fixtures.summary_fixtures import (
    seed_summary_document,
    summary_llm_responder,
)


def _responder(intent: str):
    base = summary_llm_responder("Employee Handbook")

    def _inner(messages):
        system = messages[0].content if messages else ""
        if "classify a user's question" in system:
            return json.dumps({
                "intent": intent,
                "temporal_scope": None,
                "scope_hints": [],
                "topic": "summary_request" if intent == "SUMMARY" else "terms",
            })
        return base(messages)

    return _inner


@pytest_asyncio.fixture()
async def chat_env(
    app_session_factory: async_sessionmaker,
    redis_client,
    async_redis_url: str,
    monkeypatch,
) -> async_sessionmaker:
    pool = await create_pool(RedisSettings.from_dsn(async_redis_url))
    await pool.flushdb()
    monkeypatch.setattr(queue_mod, "_arq_pool", pool)
    monkeypatch.setattr(db_mod, "_session_factory", app_session_factory)
    set_llm_provider(StubLLMProvider(responder=_responder("SUMMARY")))
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    yield app_session_factory
    set_llm_provider(None)
    set_embedding_provider(None)
    await pool.aclose()


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client) -> tuple[str, str]:
    slug = f"chat-sum-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Chat Summary Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Chat Summary Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"], slug


async def _current_user_id(client, token: str) -> str:
    resp = await client.get("/auth/me", headers=_auth_headers(token))
    return resp.json()["id"]


async def _org_id(factory, user_id: str) -> str:
    async with factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"), {"id": user_id}
        )
        return str(row.scalar_one())


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


async def _single_job_id(factory) -> str:
    async with factory() as session:
        return str((await session.execute(
            text("SELECT id FROM processing_jobs")
        )).scalars().first())


@pytest.mark.integration
class TestChatSummaryRouting:

    @pytest.mark.asyncio
    async def test_summary_intent_full_round_trip(
        self, app_client: AsyncClient, app_session_factory, chat_env
    ):
        """Exit criterion 2: "Summarize this policy" routes to SummaryService
        and — once the worker finished — returns the sectioned, cited result
        persisted as a normal ASSISTANT message."""
        factory = chat_env
        token, _slug = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _org_id(factory, user_id)
        doc = await seed_summary_document(
            factory, organization_id=org_id, owner_id=user_id
        )

        # 1. First ask → summary created; the stream acknowledges (never blocks)
        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "Summarize this policy.",
                    "scope": {
                        "type": "current_document",
                        "document_ids": [doc["document_id"]],
                    },
                }
            },
        )
        assert events[0][0] == "start"
        conversation_id = events[0][1]["conversation_id"]
        assert "Generating a summary" in _done_answer(events)

        # 2. The worker completes the summary
        job_id = await _single_job_id(factory)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        # 3. Re-ask in the SAME conversation → the narrated persisted summary.
        #    Exit criterion 5: identical SSE contract (start → citation* → done).
        events2 = await _stream_sse(
            app_client, token, "POST",
            f"/chat/conversations/{conversation_id}/messages",
            {"content": "Summarize this policy."},
        )
        event_names = [n for n, _ in events2]
        assert event_names[0] == "start"
        assert event_names[-1] == "done"
        answer = _done_answer(events2)
        assert "twenty days of annual leave" in answer

        # The narrated answer PERSISTED as a normal ASSISTANT message +
        # citation rows (re-hydrated from the summary's stored citations).
        async with factory() as session:
            row = await session.execute(text(
                "SELECT m.content, count(c.id) FROM messages m "
                "LEFT JOIN citations c ON c.message_id = m.id "
                "WHERE m.conversation_id = :cid AND m.role = 'ASSISTANT' "
                "GROUP BY m.content, m.created_at ORDER BY m.created_at DESC LIMIT 1"
            ), {"cid": conversation_id})
            content, citation_rows = row.first()
        assert "annual leave" in content
        assert citation_rows >= 1

    @pytest.mark.asyncio
    async def test_no_document_in_scope_clarifies(
        self, app_client: AsyncClient, chat_env
    ):
        """Zero documents in scope → persisted clarifying turn, never a
        fabricated summary and never a job."""
        token, _ = await _register_and_login(app_client)

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "Summarize this policy.",
                    "scope": {"type": "knowledge_base", "document_ids": []},
                }
            },
        )
        answer = _done_answer(events)
        assert "exactly one document" in answer
        # No SUMMARY job was created
        async with chat_env() as session:
            count = (await session.execute(
                text("SELECT count(*) FROM processing_jobs")
            )).scalar_one()
        assert count == 0


@pytest.mark.integration
class TestChatExtractionRouting:

    @pytest.mark.asyncio
    async def test_extraction_intent_reuses_completed_run(
        self, app_client: AsyncClient, app_session_factory, chat_env, monkeypatch
    ):
        """§2.6 point 5 via chat: first ask creates a run + acknowledgement;
        after completion, re-asking REUSES the run (no new job) and narrates
        its items."""
        from tests.fixtures.extraction_fixtures import (
            extraction_llm_responder,
            seed_contract_document,
        )

        # Point the analyzer+builder stubs at the EXTRACTION answers.
        set_llm_provider(StubLLMProvider(responder=extraction_llm_responder()))

        factory = chat_env
        token, _slug = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _org_id(factory, user_id)
        doc = await seed_contract_document(
            factory, organization_id=org_id, owner_id=user_id
        )

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "What are the requirements in this contract?",
                    "scope": {
                        "type": "current_document",
                        "document_ids": [doc["document_id"]],
                    },
                }
            },
        )
        conversation_id = events[0][1]["conversation_id"]
        assert "Running the structured extraction" in _done_answer(events)

        job_id = await _single_job_id(factory)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        events2 = await _stream_sse(
            app_client, token, "POST",
            f"/chat/conversations/{conversation_id}/messages",
            {"content": "What are the requirements in this contract?"},
        )
        answer2 = _done_answer(events2)
        assert answer2  # narrated or fallback — never empty

        # Exactly ONE extraction run exists — the chat reused it.
        async with factory() as session:
            count = (await session.execute(
                text("SELECT count(*) FROM document_extractions")
            )).scalar_one()
        assert count == 1
