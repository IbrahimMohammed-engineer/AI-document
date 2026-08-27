# AI Document Intelligence Platform

An enterprise, multi-tenant document intelligence platform for uploading, organizing, searching, questioning, comparing, and analyzing business documents — where every AI answer is grounded in retrievable, citable source evidence.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React + TypeScript + Vite + Tailwind CSS |
| Backend | FastAPI (Python 3.11+) |
| Database | PostgreSQL 16 + pgvector |
| Queue | Redis + Arq |
| Object Storage | MinIO (dev) / S3 or Azure Blob (prod) |
| ORM | SQLAlchemy 2.x (async) + Alembic |

## Quick Start (Development)

### Prerequisites
- Docker Desktop (with Docker Compose)
- Python 3.11+ (for running migrations outside Docker)
- Node 20+ (for frontend development outside Docker)

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env — at minimum add your OPENAI_API_KEY (needed from Phase 7+)
```

### 2. Start the full stack

```bash
docker compose up --build
```

This starts: PostgreSQL/pgvector, Redis, MinIO, FastAPI backend, Arq worker, and the Vite frontend.

### 3. Run database migrations

```bash
# In a new terminal (or exec into the backend container)
docker compose exec backend alembic upgrade head
```

### 4. Verify everything is healthy

```bash
curl http://localhost:8000/health/ready
# → {"status": "ok", "database": "ok", "redis": "ok"}

curl http://localhost:8000/health/live
# → {"status": "ok"}
```

### Service URLs (dev)

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| MinIO Console | http://localhost:9001 |

### 5. Run tests

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Project Structure

```
AI-Document-Intelligence/
├── backend/                  # FastAPI application + Arq workers
│   ├── app/
│   │   ├── api/              # Routers + FastAPI dependencies
│   │   ├── core/             # Config, logging, exceptions
│   │   ├── domain/           # Business rules (pure Python)
│   │   ├── infrastructure/   # DB/Redis/storage/provider clients
│   │   ├── models/           # SQLAlchemy ORM models
│   │   ├── rag/              # RAG pipeline (Phase 7+)
│   │   ├── ingestion/        # Ingestion pipeline (Phase 5+)
│   │   ├── repositories/     # Repository layer
│   │   ├── schemas/          # Pydantic request/response schemas
│   │   ├── services/         # Application service layer
│   │   └── workers/          # Arq worker entrypoints
│   ├── alembic/              # Database migrations
│   └── tests/
├── frontend/                 # React + Vite frontend
│   └── src/
├── Documentation/            # Architecture specs
├── docker-compose.yml
└── .env.example
```

## Implementation Phases

| Phase | Status | Description |
|---|---|---|
| 0 | ✅ Done | Project setup, scaffold, Docker Compose |
| 1 | ✅ Done | Database foundation, migrations, repository layer |
| 2 | ⬜ Next | Authentication, authorization, multi-tenancy |
| 3 | ⬜ | Document management, object storage |
| 4 | ⬜ | Background processing (Redis/Arq) |
| 5 | ⬜ | Document extraction + OCR |
| 6 | ⬜ | Structure detection + chunking |
| 7 | ⬜ | Embeddings + pgvector |
| 8 | ⬜ | Hybrid search + reranking |
| 9 | ⬜ | Basic RAG pipeline |
| 10 | ⬜ | Citations + source validation |
| 11 | ⬜ | Conversations + streaming chat |

## Architecture Principles

1. **Layers are one-directional**: API → Service → Domain → Repository → Infrastructure
2. **Tenant isolation is structural**: every tenant-scoped repository method requires `organization_id` as an explicit parameter
3. **Document processing is always async**: HTTP requests end at `202 Accepted`; workers handle the rest
4. **Retrieved content is never trusted as instructions**: prompt injection defense
5. **Citations are never fabricated**: they resolve through the backend's context-assembly record
