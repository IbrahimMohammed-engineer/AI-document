# AI Document Intelligence Platform — Phase 0 + Phase 1 Implementation Plan

## Overview

The project currently has **no code** — only documentation. To implement Phase 1 (Database & Backend Foundation), we must first implement **Phase 0 (Project Setup)** since it is a hard prerequisite. This plan covers both phases as a single sequential implementation.

The result will be a fully working engineering scaffold: monorepo, FastAPI backend with layered architecture, SQLAlchemy 2 async engine, Alembic migrations for identity/RBAC/audit tables, repository pattern with tenant scoping, React+Vite frontend skeleton, Docker Compose stack, and CI foundations.

---

## User Review Required

> [!IMPORTANT]
> **Technology decisions locked in this phase cannot be easily changed later:**
> - **Queue library:** Arq (recommended by the docs) — used for background processing from Phase 4 onward
> - **Object storage for dev:** MinIO (S3-compatible) — production target is S3 or Azure Blob (config-selectable)
> - **Embedding model:** OpenAI `text-embedding-3-small` (1536-dim) — must be pinned before the Phase 7 pgvector migration
> - **Database:** PostgreSQL 16 with pgvector extension
>
> These are all in line with the documentation. If you want to change any of them, now is the time.

> [!WARNING]
> **No AI provider calls in Phase 0/1.** API keys for OpenAI/Anthropic are wired into config but not called. You will need `.env` with real keys when Phase 7+ is implemented.

---

## Open Questions

> [!IMPORTANT]
> **Q1:** Should the React frontend use **Tailwind CSS** (as specified in the Frontend Design Documentation §8) or Vanilla CSS (as per the web development guidelines)? The documentation explicitly calls for Tailwind — deferring to the documentation since this is a project-spec'd choice.
>
> **Q2:** For the Docker Compose stack, should the frontend be served via `vite dev` (HMR) or built and served via nginx? — Using `vite dev` for Phase 0/1 since this is a development scaffold.

---

## Proposed Changes

### Phase 0 — Project Scaffold

---

#### [NEW] Root directory structure

```
AI-Document-Intelligence/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app factory + lifespan
│   │   ├── api/                   # Routers + deps (empty skeletons)
│   │   │   ├── __init__.py
│   │   │   └── deps.py            # get_db_session, get_current_user stubs
│   │   ├── schemas/               # Pydantic request/response models
│   │   │   └── __init__.py
│   │   ├── models/                # SQLAlchemy ORM models
│   │   │   └── __init__.py
│   │   ├── services/              # Application service layer (empty)
│   │   │   └── __init__.py
│   │   ├── domain/                # Business rules, pure Python (empty)
│   │   │   └── __init__.py
│   │   ├── rag/                   # RAG pipeline (empty — Phase 7+)
│   │   │   └── __init__.py
│   │   ├── ingestion/             # Ingestion pipeline (empty — Phase 5+)
│   │   │   └── __init__.py
│   │   ├── repositories/          # Repository layer
│   │   │   ├── __init__.py
│   │   │   └── base.py            # BaseRepository with org_id scoping
│   │   ├── infrastructure/        # Concrete provider clients
│   │   │   ├── __init__.py
│   │   │   ├── database.py        # SQLAlchemy async engine + session factory
│   │   │   ├── redis.py           # Redis client stub
│   │   │   └── storage.py         # ObjectStorageProvider stub
│   │   ├── workers/               # Worker entrypoints (empty — Phase 4)
│   │   │   └── __init__.py
│   │   └── core/                  # Cross-cutting concerns
│   │       ├── __init__.py
│   │       ├── config.py          # Pydantic Settings
│   │       ├── logging.py         # Structured JSON logging + correlation ID
│   │       ├── exceptions.py      # AppException hierarchy + handlers
│   │       └── security.py        # JWT/password stubs (Phase 2)
│   ├── alembic/                   # Migration config + versions
│   │   ├── alembic.ini
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 001_extensions.py
│   │       ├── 002_identity_tenancy.py
│   │       └── 003_audit_logs.py
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   ├── api/
│   │   └── conftest.py
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── app/                   # Route pages
│   │   ├── components/            # Reusable UI components
│   │   │   ├── layout/
│   │   │   └── common/
│   │   ├── hooks/                 # Custom React hooks
│   │   ├── lib/
│   │   │   ├── api/               # Typed API client
│   │   │   └── query/             # React Query config
│   │   ├── state/                 # Zustand stores
│   │   ├── types/                 # TypeScript types
│   │   ├── main.tsx
│   │   └── App.tsx
│   ├── public/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

### Backend — Phase 0 Files

#### [NEW] backend/app/core/config.py
Pydantic Settings with typed env vars for DB URL, Redis URL, JWT secret, object storage, AI provider keys.

#### [NEW] backend/app/core/logging.py
Structured JSON logging, `X-Request-Id` middleware, contextvars correlation ID binding.

#### [NEW] backend/app/core/exceptions.py
`AppException` hierarchy: `NotFoundError`, `ForbiddenError`, `ValidationError`, `ExternalServiceError`, `InternalError`. FastAPI exception handlers producing the standard envelope `{ error: { code, message, field?, requestId } }`.

#### [NEW] backend/app/main.py
FastAPI app factory with lifespan (DB pool startup/shutdown), router registration, middleware wiring. Health endpoints: `GET /health/live` (always 200) and `GET /health/ready` (checks DB + Redis).

#### [NEW] backend/app/infrastructure/database.py
Async SQLAlchemy engine, session factory, `get_db_session` dependency, graceful shutdown.

#### [NEW] backend/app/infrastructure/redis.py
Redis client factory stub (used for rate limiting in Phase 2, queuing in Phase 4).

---

### Backend — Phase 1 Files

#### [NEW] backend/alembic/versions/001_extensions.py
Creates `pgcrypto` and `vector` PostgreSQL extensions.

#### [NEW] backend/alembic/versions/002_identity_tenancy.py
Creates tables: `organizations`, `users`, `roles`, `permissions`, `user_roles`, `role_permissions`, `refresh_tokens`. All with proper FK constraints, unique indexes, CHECK constraints.

#### [NEW] backend/alembic/versions/003_audit_logs.py
Creates `audit_logs` table (insert-only at DB role level). Revokes `UPDATE`/`DELETE` from app role.

#### [NEW] backend/app/models/organization.py
SQLAlchemy ORM model for `organizations` and `users`.

#### [NEW] backend/app/models/user.py
SQLAlchemy ORM models for `roles`, `permissions`, `user_roles`, `role_permissions`, `refresh_tokens`, `audit_logs`.

#### [NEW] backend/app/repositories/base.py
`BaseRepository` with `organization_id` scoping convention enforced by signature.

#### [NEW] backend/app/repositories/user_repository.py
`UserRepository` and `OrganizationRepository` for identity CRUD.

#### [NEW] backend/app/api/deps.py
`get_db_session` dependency; stub `get_current_user` and `require_permission` (functional in Phase 2).

---

### Frontend — Phase 0 Files

#### [NEW] frontend/src/main.tsx + App.tsx
Vite+React+TypeScript entry, React Query client setup, router skeleton with `/login` and `/app` placeholders.

#### [NEW] frontend/src/lib/api/client.ts
Typed Axios/fetch base client with error envelope parsing and correlation ID forwarding.

#### [NEW] frontend/src/components/layout/AppShell.tsx
Sidebar + header shell with nav placeholders.

---

### Infrastructure

#### [NEW] docker-compose.yml
Services: `postgres` (pgvector/pgvector:pg16), `redis`, `minio`, `backend` (uvicorn), `worker`, `frontend`. With healthchecks and volumes.

#### [NEW] .env.example
Documents every required env var (DB URL, Redis URL, JWT secret, storage creds, AI provider keys).

---

### Tests

#### [NEW] backend/tests/conftest.py
testcontainers-based Postgres+pgvector + Redis fixtures; async session factory; Alembic `upgrade head` on ephemeral DB.

#### [NEW] backend/tests/integration/test_health.py
`/health/live` → 200; `/health/ready` → 200 with DB up, 503 with DB down.

#### [NEW] backend/tests/integration/test_repositories.py
Repository round-trip: insert org + user, assert select returns only own-org rows; tenant-scoping signature test.

#### [NEW] backend/tests/integration/test_migrations.py
Alembic `upgrade head` → `downgrade base` → `upgrade head` runs clean.

---

## Verification Plan

### Automated Tests
```bash
cd backend && pytest tests/ -v
```
- Health checks pass with real DB container
- Alembic up/down/up runs clean
- Repository round-trip: create org + user, cross-org query returns nothing

### Manual Verification
1. Run `docker compose up` — all services start healthy
2. `GET /health/ready` returns `200 { "status": "ok" }` 
3. `GET /health/live` returns `200`
4. Deliberate 500 (call unknown route) returns standard `{ error: { code, message, requestId } }` envelope
5. Frontend dev server loads at `http://localhost:5173` with app shell
6. Worker container boots and idles without error
7. MinIO console accessible at `http://localhost:9001`
8. `alembic upgrade head` creates all Phase 1 tables with correct constraints
