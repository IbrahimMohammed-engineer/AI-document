"""
Integration tests — CHANGE_DETECTION / COMPARISON chat-intent routing
(Phase 12, plan §14 / §16.2).

The LLM stub discriminates calls by system prompt so one provider serves
the whole conversation turn:
  - analyzer   (system contains '{"intent"')     → CHANGE_DETECTION JSON
  - semantic   (system contains 'materiality')   → material JSON
  - narration  (system contains 'narrate')       → prose summary
The chat path is exercised through the real SSE endpoints.

Covers:
  - exactly 2 documents in scope → comparison created (PENDING notice,
    stream never blocks) → after the worker runs, re-asking returns the
    narrated, citation-backed PERSISTED result (no second job/LLM recompute)
  - 1 document + two years in the question → temporal resolution to the
    two versions of that document
  - 0 documents in scope → clarifying message, never a fabricated answer
  - 3+ documents in scope → clarifying message

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
from tests.fixtures.comparison_fixtures import seed_document_with_versions


# ── LLM stub ──────────────────────────────────────────────────────────────────

def _responder(messages):
    system = messages[0].content if messages else ""
    user = messages[-1].content if messages else ""
    if '{"intent"' in system:  # query analyzer
        if "2025" in user and "2026" in user:
            return (
                '{"intent": "CHANGE_DETECTION", "temporal_scope": {"year": 2026}, '
                '"temporal_scope_secondary": {"year": 2025}, '
                '"scope_hints": [], "topic": "changes"}'
            )
        return '{"intent": "CHANGE_DETECTION", "scope_hints": [], "topic": "changes"}'
    if "materiality" in system:  # semantic comparison
        return '{"materiality": "material", "rationale": "The deadline changed."}'
    if "narrate" in system:  # change narration
        return (
            "The approval window changed from 5 to 7 business days, and a "
            "Digital Signatures section was added."
        )
    if "verdict" in system:  # entailment (unused on comparison path)
        return '{"verdict": "yes", "reason": "supported"}'
    return "standalone query"


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
    set_llm_provider(StubLLMProvider(responder=_responder))
    yield app_session_factory
    set_llm_provider(None)
    await pool.aclose()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_and_login(client) -> str:
    slug = f"chat-cmp-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Chat Compare Org {slug}",
        "slug": slug,
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "full_name": "Chat Compare Admin",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"admin@{slug}.example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _current_user_id(client, token: str) -> str:
    resp = await client.get("/auth/me", headers=_auth_headers(token))
    assert resp.status_code == 200
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


async def _job_count(factory) -> int:
    async with factory() as session:
        return (await session.execute(
            text("SELECT count(*) FROM processing_jobs")
        )).scalar_one()


async def _single_job_id(factory) -> str:
    async with factory() as session:
        return str((await session.execute(
            text("SELECT id FROM processing_jobs")
        )).scalars().first())


# ── The two-document resolution path ─────────────────────────────────────────

@pytest.mark.integration
class TestChatChangeDetection:

    @pytest.mark.asyncio
    async def test_two_documents_in_scope_full_round_trip(
        self, app_client: AsyncClient, app_session_factory, chat_env
    ):
        factory = chat_env
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _org_id(factory, user_id)

        from datetime import date as _date

        from tests.fixtures.comparison_fixtures import SectionSpec

        doc_a, _ = await seed_document_with_versions(
            factory, organization_id=org_id, owner_id=user_id, name="Policy A"
        )
        # Doc B: distinct content so the cross-document comparison (doc A's
        # current version vs doc B's current version) detects real changes.
        doc_b, _ = await seed_document_with_versions(
            factory,
            organization_id=org_id, owner_id=user_id, name="Policy B",
            versions=[(1, _date(2025, 6, 1), [
                SectionSpec("1", "Policy B Overview",
                            "This is the entirely different policy B text."),
                SectionSpec("2", "Policy B Scope",
                            "Policy B applies to subsidiaries only."),
            ])],
        )

        # 1. First question → comparison created; the stream answers with the
        #    "comparing now" notice instead of blocking (plan §14).
        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "What changed?",
                    "scope": {
                        "type": "selected_documents",
                        "document_ids": [doc_a, doc_b],
                    },
                }
            },
        )
        assert events[0][0] == "start"
        conversation_id = events[0][1]["conversation_id"]
        answer = _done_answer(events)
        assert "Comparing these versions now" in answer

        # Exactly one comparison job was created
        assert await _job_count(factory) == 1

        # 2. The worker completes the comparison
        job_id = await _single_job_id(factory)
        assert await run_processing_job({}, job_id) == "COMPLETED"

        # 3. Re-ask in the SAME conversation → narrated, citation-backed
        #    answer served from the persisted result (no new job).
        events2 = await _stream_sse(
            app_client, token, "POST",
            f"/chat/conversations/{conversation_id}/messages",
            {"content": "What changed?"},
        )
        answer2 = _done_answer(events2)
        assert "5 to 7 business days" in answer2

        # Citation events ride the stream and the done payload
        citation_events = [e for e in events2 if e[0] == "citation"]
        assert len(citation_events) >= 1
        done_payload = next(d for n, d in events2 if n == "done")
        assert done_payload["citations"], "done must carry citations"

        # The narrated answer PERSISTED as a normal ASSISTANT message
        async with factory() as session:
            row = await session.execute(text(
                "SELECT m.content, count(c.id) FROM messages m "
                "LEFT JOIN citations c ON c.message_id = m.id "
                "WHERE m.conversation_id = :cid AND m.role = 'ASSISTANT' "
                "GROUP BY m.content, m.created_at ORDER BY m.created_at DESC LIMIT 1"
            ), {"cid": conversation_id})
            content, citation_rows = row.first()
        assert "5 to 7 business days" in content
        assert citation_rows >= 1

        # Still exactly one job — the persisted result was reused
        assert await _job_count(factory) == 1

    @pytest.mark.asyncio
    async def test_one_document_two_years_resolves_temporally(
        self, app_client: AsyncClient, app_session_factory, chat_env
    ):
        factory = chat_env
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _org_id(factory, user_id)

        doc, _ = await seed_document_with_versions(
            factory, organization_id=org_id, owner_id=user_id, name="Policy A"
        )

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "What changed between 2025 and 2026?",
                    "scope": {
                        "type": "selected_documents",
                        "document_ids": [doc],
                    },
                }
            },
        )
        answer = _done_answer(events)
        assert "Comparing these versions now" in answer

        # The comparison created spans the 2025 and 2026 versions
        async with factory() as session:
            row = await session.execute(text(
                "SELECT va.version_number, vb.version_number "
                "FROM document_comparisons dc "
                "JOIN document_versions va ON va.id = dc.document_a_version_id "
                "JOIN document_versions vb ON vb.id = dc.document_b_version_id"
            ))
            a_no, b_no = row.first()
        assert {a_no, b_no} == {1, 2}


# ── The clarifying-fallback path (never a fabricated comparison) ─────────────

@pytest.mark.integration
class TestChatChangeDetectionFallback:

    @pytest.mark.asyncio
    async def test_zero_documents_in_scope_gets_clarifying_message(
        self, app_client: AsyncClient, app_session_factory, chat_env
    ):
        factory = chat_env
        token = await _register_and_login(app_client)

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "What changed?",
                    "scope": {"type": "knowledge_base", "document_ids": []},
                }
            },
        )
        answer = _done_answer(events)
        assert "compare two document versions" in answer
        # Nothing was created
        assert await _job_count(factory) == 0

    @pytest.mark.asyncio
    async def test_three_documents_in_scope_gets_clarifying_message(
        self, app_client: AsyncClient, app_session_factory, chat_env
    ):
        factory = chat_env
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _org_id(factory, user_id)

        docs = []
        for i in range(3):
            doc, _ = await seed_document_with_versions(
                factory, organization_id=org_id, owner_id=user_id,
                name=f"Policy {i}",
            )
            docs.append(doc)

        events = await _stream_sse(
            app_client, token, "POST", "/chat/conversations",
            {
                "message": {
                    "content": "What changed?",
                    "scope": {
                        "type": "selected_documents",
                        "document_ids": docs,
                    },
                }
            },
        )
        answer = _done_answer(events)
        assert "compare two document versions" in answer
        assert await _job_count(factory) == 0
