"""
API tests — /chat conversations, streaming, stop, feedback (Phase 11).

All providers are stubbed at the infrastructure boundary (Backend §58); the
tests verify ORCHESTRATION + persistence correctness.  Requires
testcontainers (Postgres + Redis).

Tests (roadmap Phase 11 §Testing):
  - Contract: 401 unauthenticated; lazy creation requires a first message.
  - SSE event sequence on POST /chat/conversations and …/messages:
    start → token* → citation* → done (messageId, groundedness) — citations
    NEVER stream before generation + validation complete.
  - Persistence: USER message survives (own transaction, BEFORE retrieval);
    ASSISTANT + citations persist atomically; updated_at bump orders the
    conversation list.
  - SYSTEM scope-change marker when the scope override differs; identical
    override → NO duplicate marker (Backend §46 rule 18).
  - Scope re-validation: a document revoked mid-conversation → 403 BEFORE
    the stream opens — never a broader fallback.
  - Stop: the stop endpoint freezes the partial answer mid-stream
    (done.stopped=true, persisted metadata stopped=true).
  - Feedback: upsert semantics; my_feedback round-trips in the detail read.
  - Tenancy: conversations/messages of another org are 404.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from app.infrastructure.embeddings import StubEmbeddingProvider, set_embedding_provider
from app.infrastructure.llm import (
    LLMChunk,
    LLMMessage,
    StubLLMProvider,
    set_llm_provider,
)
from app.infrastructure.reranker import StubRerankerProvider, set_reranker_provider


# ── Provider stubbing ─────────────────────────────────────────────────────────

class SlowStreamingStubLLM(StubLLMProvider):
    """Streams word-by-word with a delay between chunks — wide cancellation
    window for the stop-control test."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cancelled = False

    async def _stream(self, messages: list[LLMMessage]):  # type: ignore[override]
        reply = self._reply(messages)
        words = reply.split(" ")
        for i, word in enumerate(words):
            piece = word if i == 0 else f" {word}"
            yield LLMChunk(
                delta=piece,
                finish_reason="stop" if i == len(words) - 1 else None,
            )
            await asyncio.sleep(0.05)


@pytest.fixture(autouse=True, scope="module")
def _stub_providers():
    set_embedding_provider(StubEmbeddingProvider(dimensions=1536))
    set_reranker_provider(StubRerankerProvider())
    set_llm_provider(StubLLMProvider())
    yield
    set_reranker_provider(None)
    set_llm_provider(None)


@pytest.fixture()
def app_redis_patch(redis_client):
    """Point the app-global Redis client at the test container."""
    from app.infrastructure import redis as redis_module

    original = redis_module._redis_client
    redis_module._redis_client = redis_client
    yield redis_client
    redis_module._redis_client = original


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _uuid() -> str:
    return str(uuid.uuid4())


async def _register_and_login(client) -> str:
    slug = f"chat-test-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/auth/register", json={
        "org_name": f"Chat Test Org {slug}",
        "slug": slug,
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "full_name": "Chat Tester",
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/auth/login", json={
        "email": f"{slug}@example.com",
        "password": "TestPassword123!",
        "org_slug": slug,
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def _current_user_id(client, token: str) -> str:
    resp = await client.get("/auth/me", headers=_auth_headers(token))
    assert resp.status_code == 200
    return resp.json()["id"]


async def _get_org_id(app_session_factory, user_id: str) -> str:
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT organization_id FROM users WHERE id = :id"),
            {"id": user_id},
        )
        return str(row.scalar_one())


async def _seed_document_with_chunks(
    app_session_factory,
    org_id: str,
    owner_id: str,
    chunks: list[str],
    *,
    document_name: str = "Marketing Policy 2026",
    access_level: str = "organization",
) -> str:
    doc_id, ver_id, page_id = _uuid(), _uuid(), _uuid()
    async with app_session_factory() as session:
        await session.execute(text(
            "INSERT INTO documents (id, organization_id, owner_id, name, document_type, "
            "status, access_level) VALUES (:id, :org, :owner, :name, 'policy', "
            "'active', :access)"
        ), {"id": doc_id, "org": org_id, "owner": owner_id, "name": document_name,
            "access": access_level})
        await session.execute(text(
            "INSERT INTO document_versions (id, document_id, version_number, storage_key, "
            "mime_type, file_size_bytes, status, created_by) "
            "VALUES (:id, :doc, 1, 'key', 'application/pdf', 100, 'READY', :owner)"
        ), {"id": ver_id, "doc": doc_id, "owner": owner_id})
        await session.execute(text(
            "INSERT INTO document_pages (id, document_version_id, page_number, text) "
            "VALUES (:id, :ver, 1, 'page content')"
        ), {"id": page_id, "ver": ver_id})
        embedder = StubEmbeddingProvider(dimensions=1536)
        for idx, content in enumerate(chunks):
            vector = (await embedder.embed([content]))[0]
            vec_str = "[" + ",".join(str(x) for x in vector) + "]"
            await session.execute(text(
                "INSERT INTO document_chunks (id, document_version_id, organization_id, "
                "page_id, chunk_index, content, content_hash, token_count) "
                "VALUES (:id, :ver, :org, :page, :idx, :content, :hash, 12)"
            ), {"id": _uuid(), "ver": ver_id, "org": org_id, "page": page_id,
                "idx": idx, "content": content, "hash": _uuid()})
            await session.execute(text(
                "UPDATE document_chunks SET embedding = CAST(:v AS vector), "
                "embedding_model = 'stub' WHERE content = :content"
            ), {"v": vec_str, "content": content})
        await session.commit()
    return doc_id


async def _stream_chat_live(
    app, token: str, path: str, payload: dict,
) -> tuple[asyncio.Event, list[tuple[str, dict]], asyncio.Task]:
    """Drive the ASGI app DIRECTLY so SSE events are observable LIVE.

    httpx's ASGITransport buffers the whole response before returning it,
    which makes mid-stream races (the stop control) unobservable.  This
    helper runs the app as a task, parses `response.body` messages as the
    server emits them, and exposes an event to await the next parsed SSE
    event.  The receive channel holds the connection open (no spurious
    disconnect) until the task completes.
    """
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "headers": [
            (b"host", b"testserver"),
            (b"accept", b"text/event-stream"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ],
    }

    state = {
        "status": None,
        "event": asyncio.Event(),
        "finished": asyncio.Event(),
        "body_sent": False,
    }
    events: list[tuple[str, dict]] = []
    buffer = ""

    async def receive() -> dict:
        if not state["body_sent"]:
            state["body_sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Hold the connection open — is_disconnected() must stay False
        await state["finished"].wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal buffer
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
        elif message["type"] == "http.response.body":
            buffer += message.get("body", b"").decode("utf-8", "ignore")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                events.extend(_parse_sse(block))
                state["event"].set()
            if not message.get("more_body", False):
                state["finished"].set()

    async def _run() -> None:
        try:
            await app(scope, receive, send)
        finally:
            state["finished"].set()
            state["event"].set()

    task = asyncio.create_task(_run())
    return state, events, task


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event_name = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if event_name:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event_name, data))
    return events


async def _stream_chat(
    client, token: str, path: str, payload: dict
) -> tuple[int, list[tuple[str, dict]]]:
    """POST a chat endpoint and collect (event, data) pairs from the stream."""
    async with client.stream(
        "POST", path, json=payload, headers=_auth_headers(token)
    ) as response:
        status = response.status_code
        raw = ""
        async for chunk in response.aiter_text():
            raw += chunk
    return status, _parse_sse(raw)


async def _start_conversation(
    client, token: str, question: str, *, document_ids: list[str] | None = None,
) -> tuple[list[tuple[str, dict]], str]:
    payload: dict = {"message": {"content": question}}
    if document_ids:
        payload["message"]["scope"] = {
            "type": "selected_documents", "document_ids": document_ids,
        }
    status, events = await _stream_chat(client, token, "/chat/conversations", payload)
    assert status == 200, events
    return events, events[0][1]["conversation_id"]


# ── Contract basics ───────────────────────────────────────────────────────────

@pytest.mark.integration
class TestChatContract:

    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.get("/chat/conversations")
        assert resp.status_code == 401
        resp = await app_client.post(
            "/chat/conversations", json={"message": {"content": "q"}}
        )
        assert resp.status_code == 401

    async def test_empty_conversation_list(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.get(
            "/chat/conversations", headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == [] and body["total"] == 0

    async def test_scope_with_inaccessible_document_is_403_no_row(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["Approval needs four stages."],
        )
        foreign_id = _uuid()  # non-existent → not accessible → 403

        resp = await app_client.post(
            "/chat/conversations",
            headers=_auth_headers(token),
            json={
                "message": {
                    "content": "What is the approval process?",
                    "scope": {
                        "type": "selected_documents",
                        "document_ids": [foreign_id],
                    },
                }
            },
        )
        assert resp.status_code == 403

        # Lazy-creation rule: the failed request left NO conversation behind
        listing = await app_client.get(
            "/chat/conversations", headers=_auth_headers(token)
        )
        assert listing.json()["total"] == 0


# ── The streamed turn + persistence ───────────────────────────────────────────

@pytest.mark.integration
class TestChatStreamingAndPersistence:

    async def test_first_message_event_sequence_and_persistence(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages: "
             "submission, manager review, compliance review, and final sign-off."],
        )

        status, events = await _stream_chat(app_client, token, "/chat/conversations", {
            "message": {"content": "What is the approval process?"},
        })
        assert status == 200

        names = [name for name, _ in events]
        assert names[0] == "start"
        assert names.count("done") == 1
        assert names[-1] == "done"
        assert "citation" in names
        # Citations come AFTER all tokens (never mid-stream — Backend §37)
        assert names.index("citation") > max(
            i for i, n in enumerate(names) if n == "token"
        )
        # No error, no /ask-style sources event on the chat protocol
        assert "error" not in names
        assert "sources" not in names

        start = events[0][1]
        assert start["conversation_id"] and start["user_message_id"]
        assert start["assistant_message_id"]
        assert start["scope"]["type"] == "knowledge_base"

        tokens = "".join(d["delta"] for n, d in events if n == "token")
        assert len(tokens) > 0

        done = events[-1][1]
        assert done["message_id"] == start["assistant_message_id"], \
            "the pre-allocated id is the persisted row's id (stop-flag match)"
        assert done["groundedness"] == "grounded"
        assert done["stopped"] is False
        assert done["answer"]  # post-validation text
        assert len(done["citations"]) >= 1
        assert len(done["sources"]) >= 1

        # ── Persistence ────────────────────────────────────────────────
        async with app_session_factory() as session:
            rows = (await session.execute(text(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = :id ORDER BY created_at"
            ), {"id": start["conversation_id"]})).mappings().all()
            assert [r["role"] for r in rows] == ["USER", "ASSISTANT"]
            assert rows[0]["content"] == "What is the approval process?"

            citations = (await session.execute(text(
                "SELECT COUNT(*) FROM citations WHERE message_id = :id"
            ), {"id": done["message_id"]})).scalar_one()
            assert citations == len(done["citations"]) >= 1

            # Auto-title from the first message (DB §20)
            conv = (await session.execute(text(
                "SELECT title, scope_type FROM conversations WHERE id = :id"
            ), {"id": start["conversation_id"]})).mappings().one()
            assert "approval process" in conv["title"].lower()
            assert conv["scope_type"] == "knowledge_base"

    async def test_second_turn_uses_history_and_bumps_recency(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages. "
             "The review stage checks compliance before final sign-off."],
        )

        _events, conv_id = await _start_conversation(
            app_client, token, "What is the approval process?"
        )
        first_updated_at = (await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )).json()["conversation"]["updated_at"]

        # The generation call must receive the prior USER/ASSISTANT turns as
        # history.  Identify the GENERATION call by its SOURCE context blocks
        # (analyzer/rewriter/entailment prompts never contain them); the
        # stub records every call via its responder hook.
        captured_calls: list[list[LLMMessage]] = []
        base_reply = StubLLMProvider()._reply

        def _capturing_responder(messages):
            captured_calls.append(list(messages))
            return base_reply(messages)

        set_llm_provider(StubLLMProvider(responder=_capturing_responder))
        try:
            status, events2 = await _stream_chat(
                app_client, token,
                f"/chat/conversations/{conv_id}/messages",
                {"content": "Explain the review stage next."},
            )
            assert status == 200
            assert events2[-1][0] == "done"
        finally:
            set_llm_provider(StubLLMProvider())

        generation_calls = [
            msgs for msgs in captured_calls
            if any("SOURCE 1" in m.content for m in msgs)
        ]
        assert generation_calls, "generation prompt must carry SOURCE context"
        roles = [m.role for m in generation_calls[-1]]
        assert "assistant" in roles, "the conversation history feeds generation"
        assert "user" in roles

        detail = (await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )).json()
        assert detail["conversation"]["updated_at"] > first_updated_at
        assert detail["total_messages"] == 4  # USER+ASSISTANT ×2
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["USER", "ASSISTANT", "USER", "ASSISTANT"]

    async def test_scope_change_records_system_marker(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        doc_a = await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["Policy A explains the approval timeline and its four stages in detail."],
            document_name="Policy A",
        )
        doc_b = await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["Policy B covers the quarterly review cycle and its review stages."],
            document_name="Policy B",
        )

        _, conv_id = await _start_conversation(
            app_client, token, "What is the approval process?",
            document_ids=[doc_a],
        )

        # Override to a DIFFERENT document set → SYSTEM marker (§46 rule 18)
        status, events = await _stream_chat(
            app_client, token,
            f"/chat/conversations/{conv_id}/messages",
            {
                "content": "What does Policy B say about the review stages?",
                "scope": {
                    "type": "selected_documents",
                    "document_ids": [doc_b],
                },
            },
        )
        assert status == 200
        assert events[-1][1]["groundedness"] in ("grounded", "partial")

        detail = (await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )).json()
        roles = [m["role"] for m in detail["messages"]]
        assert "SYSTEM" in roles, "scope changes are visible markers"
        marker = next(m for m in detail["messages"] if m["role"] == "SYSTEM")
        assert "Selected documents" in marker["content"]
        assert detail["conversation"]["scope_type"] == "selected_documents"

        # conversation_documents: doc_a removed (row retained), doc_b active
        async with app_session_factory() as session:
            rows = (await session.execute(text(
                "SELECT document_id, removed_at FROM conversation_documents "
                "WHERE conversation_id = :id"
            ), {"id": conv_id})).mappings().all()
            assert len(rows) == 2, "removals are recorded, not deleted"
            active = {
                str(r["document_id"]) for r in rows if r["removed_at"] is None
            }
            assert active == {doc_b}

        # An IDENTICAL override produces NO duplicate marker
        await _stream_chat(
            app_client, token,
            f"/chat/conversations/{conv_id}/messages",
            {
                "content": "More detail on the review stages please.",
                "scope": {
                    "type": "selected_documents",
                    "document_ids": [doc_b],
                },
            },
        )
        detail2 = (await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )).json()
        assert detail2["messages"].count(marker) == 1

    async def test_revoked_scope_document_is_403_before_stream(
        self, app_client, app_session_factory
    ):
        """Scope re-validation per message (Backend §46 rule 3): revoke access
        mid-conversation → 403 short-circuit, never a broader fallback."""
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        doc_id = await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages."],
            access_level="private",  # owner-only — the owner chats fine
        )

        _, conv_id = await _start_conversation(
            app_client, token, "What is the approval process?",
            document_ids=[doc_id],
        )

        # Revoke: transfer the document to ANOTHER real user (simulates an
        # access change; private + non-owner = inaccessible).
        other_user_id = _uuid()
        async with app_session_factory() as session:
            await session.execute(text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "full_name, is_active) VALUES (:id, :org, :email, 'x', 'New Owner', true)"
            ), {"id": other_user_id, "org": org_id,
                "email": f"{uuid.uuid4().hex[:8]}@chat.test"})
            await session.execute(text(
                "UPDATE documents SET owner_id = :new_owner WHERE id = :id"
            ), {"new_owner": other_user_id, "id": doc_id})
            await session.commit()

        resp = await app_client.post(
            f"/chat/conversations/{conv_id}/messages",
            headers=_auth_headers(token),
            json={"content": "Follow-up question?"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "no longer accessible" in body["error"]["message"]

    async def test_unknown_conversation_is_404(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            f"/chat/conversations/{_uuid()}/messages",
            headers=_auth_headers(token),
            json={"content": "q"},
        )
        assert resp.status_code == 404


# ── Stop control ──────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestStopControl:

    async def test_stop_freezes_partial_answer(
        self, app_client, app_session_factory, app_redis_patch
    ):
        from app.main import app as fastapi_app

        full_reply = (
            "The documents explain the process step by step. [1] "
            "Additional elaboration follows here for the streaming window."
        )
        slow = SlowStreamingStubLLM(responder=lambda messages: full_reply)
        set_llm_provider(slow)
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages."],
        )

        # Drive the ASGI app directly — the start event is observable LIVE,
        # so the stop request can race a genuinely mid-flight generation.
        state, collected, task = await _stream_chat_live(
            fastapi_app, token, "/chat/conversations",
            {"message": {"content": "Explain the approval process."}},
        )

        # Wait for the start event to announce the assistant message id
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while loop.time() < deadline and not any(
            n == "start" for n, _ in collected
        ):
            await asyncio.wait_for(state["event"].wait(), timeout=5)
            state["event"].clear()
        starts = [d for n, d in collected if n == "start"]
        assert starts, "stream must announce the assistant id first"
        assistant_id = starts[0]["assistant_message_id"]

        # Wait for the first token (retrieval precedes generation), then stop
        # mid-generation — 17 words at 0.05 s each leaves a wide window.
        deadline = loop.time() + 15
        while loop.time() < deadline and not any(
            n == "token" for n, _ in collected
        ):
            await asyncio.wait_for(state["event"].wait(), timeout=5)
            state["event"].clear()
        assert any(n == "token" for n, _ in collected), "tokens must be flowing"
        stop_resp = await app_client.post(
            f"/chat/messages/{assistant_id}/stop", headers=_auth_headers(token)
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["stopped"] is True

        await asyncio.wait_for(task, timeout=60)
        assert state["status"] == 200
        names = [n for n, _ in collected]
        assert names[-1] == "done"
        done = collected[-1][1]
        assert done["stopped"] is True
        assert done["message_id"] == assistant_id
        tokens = "".join(d["delta"] for n, d in collected if n == "token")
        assert 0 < len(tokens) < len(full_reply), "generation was cut short"

        # The frozen partial is the persisted final text, metadata stopped
        async with app_session_factory() as session:
            row = (await session.execute(text(
                "SELECT content, metadata FROM messages WHERE id = :id"
            ), {"id": done["message_id"]})).mappings().one()
            assert row["metadata"]["stopped"] is True
            assert row["metadata"]["stop_reason"] == "user"
            assert row["content"] == done["answer"]

    async def test_stop_in_flight_id_flag_is_inert_after_finish(
        self, app_client, app_redis_patch
    ):
        """An id that was never persisted (no live stream) accepts the flag —
        it simply expires without effect (roadmap: short-TTL, message-scoped)."""
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            f"/chat/messages/{_uuid()}/stop", headers=_auth_headers(token)
        )
        assert resp.status_code == 200
        assert resp.json()["stopped"] is True

    async def test_stop_malformed_id_is_404(self, app_client):
        token = await _register_and_login(app_client)
        resp = await app_client.post(
            "/chat/messages/not-a-uuid/stop", headers=_auth_headers(token)
        )
        assert resp.status_code == 404


# ── Feedback ──────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestFeedback:

    async def test_feedback_upsert_and_detail_round_trip(
        self, app_client, app_session_factory
    ):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages."],
        )
        events, conv_id = await _start_conversation(
            app_client, token, "What is the approval process?"
        )
        message_id = events[-1][1]["message_id"]

        resp = await app_client.post(
            f"/chat/messages/{message_id}/feedback",
            headers=_auth_headers(token),
            json={"rating": 1},
        )
        assert resp.status_code == 200
        assert resp.json()["rating"] == 1

        # Resubmission updates (DB §21 unique) — not a duplicate
        resp = await app_client.post(
            f"/chat/messages/{message_id}/feedback",
            headers=_auth_headers(token),
            json={"rating": -1, "comment": "incomplete"},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "message_id": message_id, "rating": -1, "comment": "incomplete"
        }

        detail = (await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )).json()
        rated = next(
            m for m in detail["messages"] if m["id"] == message_id
        )
        assert rated["my_feedback"] == -1

    async def test_rating_validation(self, app_client, app_session_factory):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages."],
        )
        events, _conv = await _start_conversation(
            app_client, token, "What is the approval process?"
        )
        message_id = events[-1][1]["message_id"]
        resp = await app_client.post(
            f"/chat/messages/{message_id}/feedback",
            headers=_auth_headers(token),
            json={"rating": 0},
        )
        assert resp.status_code == 422

    async def test_foreign_org_message_is_404(
        self, app_client, app_session_factory
    ):
        token_a = await _register_and_login(app_client)
        user_a = await _current_user_id(app_client, token_a)
        org_a = await _get_org_id(app_session_factory, user_a)
        await _seed_document_with_chunks(
            app_session_factory, org_a, user_a,
            ["The approval process requires four sequential stages."],
        )
        events, _conv = await _start_conversation(
            app_client, token_a, "What is the approval process?"
        )
        message_id = events[-1][1]["message_id"]

        token_b = await _register_and_login(app_client)  # different org
        resp = await app_client.post(
            f"/chat/messages/{message_id}/feedback",
            headers=_auth_headers(token_b),
            json={"rating": 1},
        )
        assert resp.status_code == 404
        resp = await app_client.post(
            f"/chat/messages/{message_id}/stop", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 404


# ── Archive + tenancy of reads ────────────────────────────────────────────────

@pytest.mark.integration
class TestArchiveAndTenancy:

    async def test_delete_conversation_hides_it(self, app_client, app_session_factory):
        token = await _register_and_login(app_client)
        user_id = await _current_user_id(app_client, token)
        org_id = await _get_org_id(app_session_factory, user_id)
        await _seed_document_with_chunks(
            app_session_factory, org_id, user_id,
            ["The approval process requires four sequential stages."],
        )
        _events, conv_id = await _start_conversation(
            app_client, token, "What is the approval process?"
        )

        resp = await app_client.delete(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )
        assert resp.status_code == 204

        listing = (await app_client.get(
            "/chat/conversations", headers=_auth_headers(token)
        )).json()
        assert listing["total"] == 0
        resp = await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token)
        )
        assert resp.status_code == 404

    async def test_foreign_org_conversation_is_404(
        self, app_client, app_session_factory
    ):
        token_a = await _register_and_login(app_client)
        user_a = await _current_user_id(app_client, token_a)
        org_a = await _get_org_id(app_session_factory, user_a)
        await _seed_document_with_chunks(
            app_session_factory, org_a, user_a,
            ["The approval process requires four sequential stages."],
        )
        _events, conv_id = await _start_conversation(
            app_client, token_a, "What is the approval process?"
        )

        token_b = await _register_and_login(app_client)
        resp = await app_client.get(
            f"/chat/conversations/{conv_id}", headers=_auth_headers(token_b)
        )
        assert resp.status_code == 404
