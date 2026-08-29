"""
Integration tests â€” conversations, scopes, markers, feedback, relay (Phase 11).

Requires testcontainers (real Postgres with migrations applied through 011
+ Redis for the relay/stop-flag suites).

Tests (roadmap Phase 11 Â§Testing â€” integration row):
  - Conversation + scoped-message persistence: lazy creation, USER/ASSISTANT/
    SYSTEM rows, updated_at bump, soft delete excluded from lists.
  - conversation_documents diff semantics: scope changes are RECORDED
    (removed_at set / reset), never silently mutated (DB Â§21).
  - Message history window: bounded USER/ASSISTANT pairs, SYSTEM excluded.
  - message_feedback upsert: one rating per user per message (DB Â§21).
  - messages.conversation_id FK: deleting a conversation cascades messages.
  - Redis relay: publish â†’ subscribe round-trip; stop flags set/clear.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.infrastructure import stop_flags
from app.infrastructure.relay import (
    chat_channel,
    job_channel,
    publish,
    subscribe_events,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_feedback_repository import MessageFeedbackRepository
from app.repositories.message_repository import MessageRepository


@pytest.fixture()
def _patched_app_redis(redis_client):
    """Point the app-global Redis client at the test container.

    stop_flags and the relay read the module-global client (initialized by
    the app lifespan, which does not run under the test transport) â€” these
    tests patch it directly.
    """
    from app.infrastructure import redis as redis_module

    original = redis_module._redis_client
    redis_module._redis_client = redis_client
    yield redis_client
    redis_module._redis_client = original


# â”€â”€ Seed helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _uuid() -> str:
    return str(uuid.uuid4())


async def _create_org_and_user(session_factory) -> tuple[str, str]:
    org_id, user_id = _uuid(), _uuid()
    async with session_factory() as session:
        await session.execute(text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, 'Chat Org', :slug)"
        ), {"id": org_id, "slug": f"chat-{uuid.uuid4().hex[:8]}"})
        await session.execute(text(
            "INSERT INTO users (id, organization_id, email, password_hash, "
            "full_name, is_active) VALUES (:id, :org, :email, 'x', 'Tester', true)"
        ), {"id": user_id, "org": org_id,
            "email": f"{uuid.uuid4().hex[:8]}@chat.test"})
        await session.commit()
    return org_id, user_id


async def _seed_documents(session_factory, org_id: str, owner_id: str, n: int = 2) -> list[str]:
    doc_ids = []
    async with session_factory() as session:
        for i in range(n):
            doc_id = _uuid()
            doc_ids.append(doc_id)
            await session.execute(text(
                "INSERT INTO documents (id, organization_id, owner_id, name, "
                "document_type, status, access_level) "
                "VALUES (:id, :org, :owner, :name, 'policy', 'active', 'organization')"
            ), {"id": doc_id, "org": org_id, "owner": owner_id,
                "name": f"Doc {i} {doc_id[:6]}"})
        await session.commit()
    return doc_ids


async def _create_conversation(
    session_factory, org_id: str, user_id: str,
    *, scope_type: str = "selected_documents", doc_ids: list[str] | None = None,
) -> str:
    async with session_factory() as session:
        repo = ConversationRepository(session)
        conversation = await repo.create(
            organization_id=org_id,
            user_id=user_id,
            title="Test conversation",
            scope_type=scope_type,
        )
        if scope_type != "knowledge_base" and doc_ids:
            await repo.set_scope_documents(conversation.id, doc_ids)
        await session.commit()
        return conversation.id


# â”€â”€ Conversation persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
class TestConversationPersistence:

    async def test_conversation_round_trip_and_recency_order(
        self, app_session_factory
    ):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        doc_ids = await _seed_documents(app_session_factory, org_id, user_id, 1)

        conv_a = await _create_conversation(
            app_session_factory, org_id, user_id, doc_ids=doc_ids
        )
        conv_b = await _create_conversation(
            app_session_factory, org_id, user_id, doc_ids=doc_ids
        )

        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            # Messages bump updated_at â†’ recency ordering (DB Â§27 index)
            await MessageRepository(session).save_user_message(
                conversation_id=conv_b, content="latest question"
            )
            await repo.bump_updated_at(conv_b)
            await session.commit()

        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            conversations = await repo.list_for_user(org_id, user_id)
            assert [c.id for c in conversations] == [conv_b, conv_a]

            fetched = await repo.get_for_user(conv_a, org_id, user_id)
            assert fetched is not None
            assert fetched.scope_type == "selected_documents"
            assert await repo.active_document_ids(conv_a) == doc_ids

    async def test_other_user_cannot_read_conversation(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        doc_ids = await _seed_documents(app_session_factory, org_id, user_id, 1)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, doc_ids=doc_ids
        )

        # A second user in the SAME org â€” conversations are private in V1
        async with app_session_factory() as session:
            await session.execute(text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "full_name, is_active) VALUES (:id, :org, :email, 'x', 'Other', true)"
            ), {"id": _uuid(), "org": org_id,
                "email": f"{uuid.uuid4().hex[:8]}@chat.test"})
            other_id = (await session.execute(text(
                "SELECT id FROM users WHERE organization_id = :org "
                "AND id != :owner"
            ), {"org": org_id, "owner": user_id})).scalar_one()
            await session.commit()

        async with app_session_factory() as session:
            fetched = await ConversationRepository(session).get_for_user(
                conv_id, org_id, other_id
            )
            assert fetched is None, "conversations are private to their owner"

    async def test_soft_delete_excludes_from_list(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        doc_ids = await _seed_documents(app_session_factory, org_id, user_id, 1)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, doc_ids=doc_ids
        )

        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            conversation = await repo.get_for_user(conv_id, org_id, user_id)
            await repo.soft_delete(conversation)
            await session.commit()

        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            assert await repo.list_for_user(org_id, user_id) == []
            assert await repo.get_for_user(conv_id, org_id, user_id) is None

    async def test_deleting_conversation_cascades_messages(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id,
            scope_type="knowledge_base",
        )
        async with app_session_factory() as session:
            repo = MessageRepository(session)
            await repo.save_user_message(conversation_id=conv_id, content="q")
            await repo.save_system_message(
                conversation_id=conv_id, content="Scope changed to X."
            )
            await session.commit()

        async with app_session_factory() as session:
            await session.execute(text(
                "DELETE FROM conversations WHERE id = :id"
            ), {"id": conv_id})
            await session.commit()
            remaining = (await session.execute(text(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = :id"
            ), {"id": conv_id})).scalar_one()
            assert remaining == 0, "messages cascade with their conversation"


# â”€â”€ Scope-document diff semantics (DB Â§21 â€” recorded, not mutated) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
class TestScopeDocumentDiff:

    async def test_scope_changes_are_recorded_not_mutated(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        doc_ids = await _seed_documents(app_session_factory, org_id, user_id, 3)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, doc_ids=doc_ids[:2]
        )

        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            await repo.set_scope_documents(conv_id, [doc_ids[0], doc_ids[2]])
            await session.commit()

            rows = (await session.execute(text(
                "SELECT document_id, added_at, removed_at "
                "FROM conversation_documents WHERE conversation_id = :id "
                "ORDER BY document_id"
            ), {"id": conv_id})).mappings().all()
            by_doc = {str(r["document_id"]): r for r in rows}

            # 3 rows total â€” removals are recorded via removed_at, never deleted
            assert len(rows) == 3
            assert by_doc[doc_ids[0]]["removed_at"] is None
            assert by_doc[doc_ids[1]]["removed_at"] is not None, \
                "unchecked document keeps its row with removed_at set (DB Â§21)"
            assert by_doc[doc_ids[2]]["removed_at"] is None

            assert set(await repo.active_document_ids(conv_id)) == {
                doc_ids[0], doc_ids[2]
            }

        # Re-checking a previously removed document RESETS removed_at
        async with app_session_factory() as session:
            repo = ConversationRepository(session)
            await repo.set_scope_documents(conv_id, list(doc_ids))
            await session.commit()
            rows = (await session.execute(text(
                "SELECT document_id, removed_at FROM conversation_documents "
                "WHERE conversation_id = :id"
            ), {"id": conv_id})).mappings().all()
            assert len(rows) == 3
            assert all(r["removed_at"] is None for r in rows)
            assert set(await repo.active_document_ids(conv_id)) == set(doc_ids)


# â”€â”€ Message history window (bounded, SYSTEM excluded) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
class TestHistoryWindow:

    async def test_history_pairs_bounded_and_system_excluded(
        self, app_session_factory
    ):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, scope_type="knowledge_base"
        )
        async with app_session_factory() as session:
            repo = MessageRepository(session)
            await repo.save_user_message(conversation_id=conv_id, content="q1")
            await repo.save_assistant_message_with_citations(
                content="a1", groundedness="grounded", citations=[],
                conversation_id=conv_id,
            )
            await repo.save_system_message(
                conversation_id=conv_id, content="Scope changedâ€¦"
            )
            await repo.save_user_message(conversation_id=conv_id, content="q2")
            await repo.save_user_message(conversation_id=conv_id, content="q3")
            await session.commit()

        async with app_session_factory() as session:
            rows = await MessageRepository(session).list_history_pairs(
                conv_id, max_messages=4
            )
            contents = [r.content for r in rows]
            # SYSTEM excluded; oldestâ†’newest; bounded to the most recent 4
            assert contents == ["a1", "q2", "q3"] or contents == ["q1", "a1", "q2", "q3"]
            assert all(r.role in ("USER", "ASSISTANT") for r in rows)
            assert "Scope changedâ€¦" not in contents

    async def test_citations_grouped_by_message(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, scope_type="knowledge_base"
        )
        async with app_session_factory() as session:
            repo = MessageRepository(session)
            await repo.save_user_message(conversation_id=conv_id, content="q")
            await repo.save_assistant_message_with_citations(
                content="a", groundedness="grounded", citations=[],
                conversation_id=conv_id,
            )
            await session.commit()
            rows = await repo.list_for_conversation(conv_id)
            grouped = await repo.citations_for_messages([r.id for r in rows])
            assert grouped == {}, "no citations exist yet â€” empty map"


# â”€â”€ Feedback upsert (one rating per user per message â€” DB Â§21) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
class TestFeedbackUpsert:

    async def test_resubmission_updates_not_duplicates(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, scope_type="knowledge_base"
        )
        async with app_session_factory() as session:
            message = await MessageRepository(session).save_assistant_message_with_citations(
                content="a", groundedness="grounded", citations=[],
                conversation_id=conv_id,
            )
            await session.commit()

        async with app_session_factory() as session:
            repo = MessageFeedbackRepository(session)
            first = await repo.upsert(
                message_id=message.id, user_id=user_id, rating=1,
                comment=None,
            )
            second = await repo.upsert(
                message_id=message.id, user_id=user_id, rating=-1,
                comment="missed the nuance",
            )
            await session.commit()
            assert first.id == second.id, "upsert keeps ONE row per (message, user)"
            assert second.rating == -1
            assert second.comment == "missed the nuance"

        async with app_session_factory() as session:
            count = (await session.execute(text(
                "SELECT COUNT(*) FROM message_feedback WHERE message_id = :id"
            ), {"id": message.id})).scalar_one()
            assert count == 1
            restored = await MessageFeedbackRepository(session).get_for_user(
                message.id, user_id
            )
            assert restored is not None and restored.rating == -1

    async def test_rating_check_constraint(self, app_session_factory):
        org_id, user_id = await _create_org_and_user(app_session_factory)
        conv_id = await _create_conversation(
            app_session_factory, org_id, user_id, scope_type="knowledge_base"
        )
        async with app_session_factory() as session:
            message = await MessageRepository(session).save_user_message(
                conversation_id=conv_id, content="q"
            )
            await session.commit()
            with pytest.raises(IntegrityError):
                await session.execute(text(
                    "INSERT INTO message_feedback (message_id, user_id, rating) "
                    "VALUES (:m, :u, 0)"
                ), {"m": message.id, "u": user_id})
                await session.rollback()


# â”€â”€ Redis relay + stop flags (Phase 11 infrastructure) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.integration
class TestRelayAndStopFlags:

    async def test_relay_publish_subscribe_round_trip(self, _patched_app_redis):
        received: list[dict] = []

        async def _consume() -> None:
            async for event in subscribe_events(
                [job_channel("v-1"), chat_channel("c-1")], poll_interval=5.0
            ):
                received.append(event)
                if len(received) >= 2:
                    break

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.3)  # let the subscription register
        await publish(job_channel("v-1"), {"type": "job_update", "n": 1})
        await publish(chat_channel("c-1"), {"type": "chat_event", "n": 2})
        await asyncio.wait_for(consumer, timeout=10)

        kinds = {e.get("type") for e in received}
        assert "job_update" in kinds and "chat_event" in kinds

    async def test_relay_poll_fallback_without_messages(self, _patched_app_redis):
        """The synthetic poll event keeps streams correct when pub/sub is silent."""
        async for event in subscribe_events(
            [job_channel("v-none")], poll_interval=0.1
        ):
            assert event.get("type") == "poll"
            break

    async def test_stop_flag_set_check_clear(self, _patched_app_redis):
        message_id = _uuid()
        assert not await stop_flags.is_stop_requested(message_id)
        assert await stop_flags.request_stop(message_id) is True
        assert await stop_flags.is_stop_requested(message_id)
        await stop_flags.clear_stop(message_id)
        assert not await stop_flags.is_stop_requested(message_id)

    async def test_stop_flags_are_message_scoped(self, _patched_app_redis):
        a, b = _uuid(), _uuid()
        await stop_flags.request_stop(a)
        assert await stop_flags.is_stop_requested(a)
        assert not await stop_flags.is_stop_requested(b), \
            "stop flags are message-scoped â€” no cross-message interference"

    async def test_stop_flag_carries_ttl(self, redis_client, _patched_app_redis):
        message_id = _uuid()
        await stop_flags.request_stop(message_id)
        ttl = await redis_client.ttl(f"chat:stop:{message_id}")
        assert 0 < ttl <= 300, "stop flags are short-TTL (no manual cleanup)"
