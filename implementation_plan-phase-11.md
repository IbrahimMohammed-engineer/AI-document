# Phase 11 — Conversations and Streaming Chat

## Overview

Phase 11 wraps the validated Phase 9–10 RAG pipeline in persistent, scoped conversations — vertical slices **M4 + M5 complete, MVP feature-complete** (Phases 0–11 = Milestones 1–4).  The ordering properties are trust properties, not implementation details (Backend Flow 4 / §37 / §38 / §50):

- **Lazy creation (DB §20)** — `POST /chat/conversations` *requires* the first message; the conversation row is persisted BECAUSE a message exists.  Empty conversations are structurally impossible.
- **USER message durability precedes retrieval** — the question persists in its OWN short transaction inside `ChatService.prepare_message` (with the `QUESTION_ASKED` audit riding the same moment, Flow 4 step 19) before any pipeline work, so a generation failure can never lose it.
- **ASSISTANT + citations persist atomically AFTER validation** — never mid-stream; the conversation's `updated_at` recency cursor bumps inside the same transaction (Flow 4 step 14).
- **Scope changes are visible SYSTEM markers, never silent mutations** (Backend §46 rule 18): `conversation_documents` rows record `removed_at` instead of deleting (DB §21), and a synthetic SYSTEM message marks the transcript.  Scope is re-validated against LIVE permissions on EVERY message — a document revoked mid-conversation yields a 403 short-circuit BEFORE the stream opens, never a broader fallback.
- **Full SSE lifecycle (Backend §37)** — `start` (conversation/user/assistant message ids + resolved scope, so the stop control can address the answer before any token) → `token`\* → `citation`\* (only after generation + validation) → `done` (`message_id`, `groundedness`, `stopped`); `error` on mid-stream failure with the question preserved.  `: keep-alive` comments every 15 s guard against proxy idle timeouts (queue+producer wrapper so heartbeats never cancel the pipeline).
- **Cancellation** — client disconnect (`request.is_disconnected()`) and the explicit stop flag (short-TTL, message-scoped Redis key via `POST /chat/messages/{id}/stop`) are both checked BETWEEN chunks; the partial text freezes as the final answer (metadata `stopped: true`), still runs citation extraction/validation (strip-only — a stopped answer never triggers the expensive regeneration; stop means stop), and persists with the pre-allocated id announced in `start`.
- **Pre-allocated assistant message id** — the UUID is minted at request start, announced in `start`, used as the stop-flag key, and forced onto the persisted row — the stop target and the final message are the same object.
- **Multi-instance relay (Backend §37)** — `infrastructure/relay.py` implements the Redis pub/sub pattern (`relay:job:{version_id}` now, `relay:chat:{conversation_id}` for future-proofing).  Channels carry WAKE events, not truth: the SSE endpoints re-read authoritative state from PostgreSQL and emit only on change, so a missed message costs one poll interval (2 s) — Redis stays disposable.
- **Processing SSE (FE §11.2)** — `GET /documents/{id}/stream` + multiplexed `GET /documents/stream?ids=…` (declared on a separate router registered BEFORE `/documents/{id}` so the bare path wins).  Authorization of every id happens before the stream opens; terminal states close the connection.
- **Feedback** — `POST /chat/messages/{id}/feedback` upserts one ±1 rating per user per message (`ON CONFLICT … DO UPDATE`, DB §21).

## Proposed Changes

### Database

#### [NEW] [`011_conversations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/versions/011_conversations.py)

`conversations` (scope_type CHECK, soft delete, `updated_at` recency; index `(organization_id, user_id, updated_at DESC)` per DB §27), `conversation_documents` (PK pair + `removed_at` recording, `document_id` index), `message_feedback` (rating CHECK, UNIQUE `(message_id, user_id)`); `messages.conversation_id` gains the CASCADE FK (column stays NULLABLE — the standalone `/ask` evaluation path persists with NULL) + a `metadata` JSONB column for `{"stopped": true}`.

### Models

#### [NEW] [`models/conversation.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/models/conversation.py)

`Conversation`, `ConversationDocument` (row-retained removals), `MessageFeedback`.

#### [MODIFY] [`models/message.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/models/message.py), [`alembic/env.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/env.py)

`conversation_id` FK + `metadata_` JSONB (keyed alias, `{"stopped": true}`); new models registered for autogenerate.

### Infrastructure

#### [NEW] [`infrastructure/stop_flags.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/stop_flags.py)

Message-scoped, short-TTL stop keys; fail-open reads (a Redis outage never breaks generation).

#### [NEW] [`infrastructure/relay.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/relay.py)

`publish` (fire-and-forget), `subscribe_events` (pub/sub + synthetic poll ticks merged via `asyncio.wait`; degrades to a pure poll loop when Redis is unavailable — streams stay correct at poll latency).

#### [MODIFY] [`repositories/processing_job_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/processing_job_repository.py)

Every job transition (`mark_processing/progress/retrying/completed/failed`) publishes a relay wake for the version's channel — single hook point, fail-safe.

### Repositories

#### [NEW] [`repositories/conversation_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/conversation_repository.py)

Org+USER-scoped reads (private in V1), recency list, `bump_updated_at`, soft delete, and the DIFF-based `set_scope_documents` (removed_at recorded, re-adds reset).

#### [NEW] [`repositories/message_feedback_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/message_feedback_repository.py)

Idempotent upsert (the row is re-SELECTed post-write so an upsert in a session already holding the identity returns the UPDATED state).

#### [MODIFY] [`repositories/message_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/message_repository.py)

`save_user_message` / `save_system_message` (own short transactions), `list_for_conversation` + `citations_for_messages` (two-query history read, no N+1), `get_chat_message_for_org` (tenant predicate through the conversation), `list_history_pairs` (bounded USER/ASSISTANT window, SYSTEM excluded), pre-allocated `message_id` support on the atomic assistant write.

### Services

#### [NEW] [`services/chat_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/chat_service.py)

Lazy creation; scope resolution + per-message re-validation (403 short-circuit); SYSTEM markers; Flow 4 ordering; bounded history; the chat event translation (`start` → `token` → `citation`\* → `done`); auto-title.

#### [MODIFY] [`services/ask_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/ask_service.py)

`conversation_id` / `assistant_message_id` / `should_cancel` parameters; between-chunk cancellation checkpoint; stopped answers freeze partial text, skip regeneration, persist with `metadata.stopped`; conversation `updated_at` bumps inside the persistence transaction; empty-partial stop terminates with no assistant row.

### API Layer

#### [NEW] [`api/chat.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/chat.py)

`GET/POST/DELETE /chat/conversations`, `GET /chat/conversations/{id}`, `POST /chat/conversations/{id}/messages` (SSE), `POST /chat/messages/{id}/stop` (accepts in-flight pre-allocated ids; hard 404 for foreign-org rows), `POST /chat/messages/{id}/feedback`.  HTTP errors (401/403/404/429) arrive before the stream opens; heartbeat wrapper keeps long stages alive.

#### [NEW] [`api/document_streams.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/document_streams.py), [`schemas/chat.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/chat.py)

Processing SSE endpoints (initial `status` event, emit-on-change, close-on-terminal); chat request/response + SSE payload schemas.

#### [MODIFY] [`main.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/main.py), [`services/audit_logger.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/audit_logger.py), [`config.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/core/config.py), [`.env.example`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/.env.example)

Stream router registered before the documents router; chat router wired; `QUESTION_ASKED` action; `sse_heartbeat_seconds` / `stream_poll_seconds` / `chat_stop_flag_ttl_seconds`.

### Frontend

#### [NEW] [`lib/realtime/sse.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/lib/realtime/sse.ts)

Shared fetch-based SSE transport (EventSource cannot send Authorization) with typed open-errors and heartbeat-comment handling.

#### [NEW] [`lib/api/chat.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/lib/api/chat.ts), [`hooks/queries/useConversations.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/hooks/queries/useConversations.ts)

Conversation list/detail/delete/stop/feedback clients + React Query hooks.

#### [MODIFY] [`hooks/queries/useDocumentProcessing.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/hooks/queries/useDocumentProcessing.ts)

`useDocumentStatus` upgrades from Phase 4 polling to the live SSE stream writing straight into the query cache; REST refetch on reconnect, 3 s polling fallback when SSE is unavailable (FE §11.3 — transparent to consumers).  `useProcessingJobs` adds the multiplexed stream for the header indicator.

#### [MODIFY] [`features/ask/AskPage.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/AskPage.tsx), [`ask.css`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/ask.css)

Conversation sidebar (recency list, new/archive), persisted transcript restore (messages + citations + feedback + stopped markers), SYSTEM scope-change markers, server-backed stop control (freeze immediately, confirm via done), thumbs-up/down feedback, same citation experience.

## Testing

- **Integration** [`test_conversations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_conversations.py) (14): conversation round-trip + recency ordering; owner-only privacy; soft-delete exclusion; conversation→messages cascade; `conversation_documents` diff semantics (removed_at recorded / reset on re-add); bounded SYSTEM-excluding history window; feedback upsert + rating CHECK; relay publish→subscribe round-trip; poll-fallback ticks; stop-flag set/scoped/TTL.
- **API** [`test_chat.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/api/test_chat.py) (16): 401 contract; 403 on inaccessible scope leaves NO conversation row (lazy-creation property); `start` → `token`\* → `citation`\* → `done` sequence with citations strictly after tokens; pre-allocated id == persisted id; USER/ASSISTANT/citation persistence + auto-title; second-turn history reaches the generation prompt; SYSTEM marker on scope change (and NO duplicate marker for an identical override); `conversation_documents` diff after override; revoked scope → 403 before the stream; stop freezes a genuinely mid-flight answer (ASGI-driven live stream, persisted `metadata.stopped`, content == done payload); in-flight-id stop semantics + malformed-id 404; feedback upsert + `my_feedback` round-trip + 422 on rating 0; foreign-org 404s; archive hides the conversation.
- **API** [`test_document_streams.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/api/test_document_streams.py) (7): READY doc → single event then close; multiplexed ids; empty ids 422; unknown/foreign-org 404 before the stream; per-item authorization on the multiplexed batch; live PROCESSING → EMBEDDING → READY transitions emitted until terminal.
- **Migrations** [`test_migrations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_migrations.py) (+2): 011 tables/constraints/FK policy/nullable `conversation_id`/recency index; `downgrade -1` → `upgrade head` cycle.
- Cleanup order in `conftest.py` + the three pipeline test files puts `message_feedback`/`conversation_documents`/`conversations` before `users` (the new FKs).

## Verification

- `pytest tests` — **514 passed, 1 skipped** (unit 353 + integration 84 + api 78; 1 skipped = testcontainers marker).  One PRE-EXISTING environment failure (`test_security.py::test_expired_token_raises_token_expired`, reproduced on the untouched tree) is unrelated to this phase.
- `ruff check` on all new/modified Phase 11 files — clean (18 repo findings are pre-existing on the base tree).
- `npm run build` (tsc + vite) — clean; `npm run lint` — zero warnings.
