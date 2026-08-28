# Phase 7 — Embeddings and Vector Search

## Overview

Phase 7 implements the embedding pipeline and permission-aware semantic vector search. It transforms the document chunks created in Phase 6 into searchable embeddings stored in PostgreSQL/pgvector, and exposes a `POST /search` API endpoint for semantic search.

Two hard constraints from the spec dominate this phase:
1. **The embedding model must be pinned before the migration runs** — `vector(1536)` was already created in migration 007 with the embedding column (Phase 6 left it NULL). Phase 7 adds the HNSW index.
2. **Every vector query carries `organization_id` and `allowed_version_set` predicates inside the query** — never filter after retrieval.

## Proposed Changes

### Infrastructure Layer

---

#### [NEW] [`embeddings.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/embeddings.py)

The `EmbeddingProvider` abstraction and concrete OpenAI implementation:
- Abstract base: `embed(texts: list[str]) -> list[list[float]]`
- `OpenAIEmbeddingProvider`: batch size 100, token-bucket rate limiter (Redis-backed, shared across workers), per-batch retry with exponential backoff
- `StubEmbeddingProvider`: for testing — deterministic fake vectors
- Process-global factory `get_embedding_provider()` / `set_embedding_provider()`
- Dimension validation at startup: fail fast if config mismatches the model's expected dimensions

---

### Migration

#### [NEW] [`008_hnsw_index.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/versions/008_hnsw_index.py)

Adds the HNSW index on `document_chunks.embedding` (migration 007 created the `vector(1536)` column but left it without the index). Built `CONCURRENTLY` as a raw `op.execute()`. Also adds cost-tracking columns to `processing_jobs`.

---

### Ingestion Layer

#### [NEW] [`embedding_stage.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/ingestion/embedding_stage.py)

The `run_embedding()` orchestrator for the EMBEDDING worker stage:
- Load unembedded chunks in order (`chunk_index` ascending)
- Resumable: skip chunks that already have `embedding IS NOT NULL`
- Batch loop (batch size from config, default 100):
  - Call `EmbeddingProvider.embed()`
  - Write each batch immediately (incremental checkpointing — a crash never re-bills completed batches)
  - Cost attribution: record `tokens_used` to the job/org
  - Mark batch progress on the job row
- Status transitions: `CHUNKING → EMBEDDING` on entry
- Error taxonomy: `EmbeddingError` (deterministic, e.g., dimension mismatch) vs transient (provider timeouts → worker's generic retry)

---

### Repository Layer

#### [MODIFY] [`document_chunk_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/document_chunk_repository.py)

Add Phase 7 methods:
- `list_unembedded_for_version(version_id)` — chunks with `embedding IS NULL` in reading order (resumability)
- `update_embeddings_batch(updates: list[dict])` — bulk-update `embedding` + `embedding_model` per chunk ID using PostgreSQL `UPDATE ... FROM VALUES`
- `semantic_search(org_id, version_ids, query_vector, top_k, ...)` — the permission-enforced ANN query:
  - `WHERE c.organization_id = :org AND c.document_version_id = ANY(:version_ids) AND c.embedding IS NOT NULL AND d.deleted_at IS NULL`
  - All inside ONE SQL statement (never filter after)
  - `ORDER BY c.embedding <=> :query_vector` (cosine distance)
  - Returns `list[ChunkSearchResult]` with doc/version/page/section context
- `count_embedded_for_version(version_id)` — progress reporting

---

### RAG Layer

#### [NEW] [`retriever.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/retriever.py)

The semantic search orchestrator:
- `SemanticRetriever.search(query, user, db, scope)`:
  1. `resolve_allowed_documents(user, scope)` — get allowed `version_ids` (empty set → short-circuit, return `[]`)
  2. Embed the query via `get_embedding_provider().embed([query])[0]`
  3. Call `chunk_repo.semantic_search(org_id, version_ids, query_vector, top_k=50)`
  4. Return ranked `SearchResult` dataclasses with normalized relevance scores

---

### Service / Authorization Layer

#### [MODIFY] [`authorization_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/authorization_service.py)

Implement `resolve_allowed_documents(user, scope, db)` fully:
- Scope types: `"all"` (org-wide) or `{"document_ids": [...]}` or `{"collection_ids": [...]}`
- For each in-scope document: check `access_level` (`organization` = all members; `restricted`/`private` = owner or explicit grant)
- For each allowed document: resolve `current_version_id` (via `DocumentVersionRepository`)
- Return `list[str]` of allowed version IDs
- Empty list → caller short-circuits (never a broader fallback)

---

### Domain Layer

#### [NEW] [`versioning.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/domain/versioning.py)

Pure domain logic for version resolution (Backend §16):
- `resolve_current_version(versions: list[DocumentVersion]) -> DocumentVersion | None` — picks the `is_current=True` row (or latest `created_at` if no flag set)
- This is pure (no I/O) and fully unit-testable

---

### Workers Layer

#### [MODIFY] [`jobs.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/workers/jobs.py)

Add Phase 7 changes:
- `handle_embedding()`: the EMBEDDING stage handler; calls `run_embedding()`; chains an INDEXING job when done
- `handle_indexing()`: lightweight verification pass — confirm all chunks have embeddings, call `EXPLAIN ANALYZE` (logged only) to verify HNSW usage, set version status to `READY` and updates `current_version_id` on the document
- Add both to `HANDLERS` registry
- Modify `handle_chunking()` to chain an EMBEDDING job (currently the chain ends after CHUNKING)

---

### API Layer

#### [NEW] [`search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/search.py)

`POST /search` endpoint (semantic mode only for Phase 7; hybrid/keyword modes arrive in Phase 8):
- Auth required (`get_current_user`)
- Request: `{ query: str, scope?: {...}, top_k?: int (5–50) }`
- Calls `SemanticRetriever.search()`
- Response: paginated ranked results with `{ chunk_id, document_id, version_id, page_number, section_title, snippet, relevance }`
- Permission-denied empty scope → `{ results: [], total: 0, message: "No documents in scope" }`

`GET /documents/{id}/chunks` (debug/internal): returns paginated chunks for a version.

---

### Schemas

#### [NEW] [`search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/search.py)

Pydantic schemas for the search request/response.

---

### Config

#### [MODIFY] [`config.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/core/config.py)

Add Phase 7 settings:
- `embedding_batch_size: int = 100` — chunks per provider call
- `embedding_rate_limit_rpm: int = 3000` — requests-per-minute ceiling (token bucket)
- `embedding_request_timeout_seconds: float = 15.0` — per-batch timeout
- `embedding_provider: Literal["openai", "stub"] = "openai"`
- `hnsw_ef_search: int = 100` — `hnsw.ef_search` session-level setting (higher = better recall, lower = lower latency)
- `search_top_k_default: int = 10`
- `search_top_k_max: int = 50`

### `main.py`

#### [MODIFY] [`main.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/main.py)

- Register search router
- Initialize embedding provider on startup

---

### Tests

#### [NEW] [`tests/unit/test_embedding_provider.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_embedding_provider.py)

Unit tests:
- Stub provider returns correct-dimension vectors
- Batch size splitting (input 250 chunks → 3 batches of 100)
- Rate-limiter token-bucket logic
- Dimension mismatch → hard error

#### [NEW] [`tests/unit/test_versioning.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_versioning.py)

Unit tests for `resolve_current_version()`.

#### [NEW] [`tests/integration/test_embedding_pipeline.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_embedding_pipeline.py)

Integration tests (testcontainers):
- Embed-and-persist round trip with the stub provider
- Crash-after-batch-2 → resume embeds only remaining chunks (assert call counts)
- HNSW recall sanity on seeded similar/dissimilar vectors
- Tenant isolation: org A query never returns org B chunks

#### [NEW] [`tests/api/test_search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/api/test_search.py)

API tests:
- Search happy path (stub provider)
- Empty-scope short-circuit
- Unauthorized → 401
- Invalid scope → 400

---

### Requirements

#### [MODIFY] [`requirements.txt`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/requirements.txt)

Add:
- `openai>=1.59.0` — OpenAI embeddings client
- `pgvector>=0.3.6` — SQLAlchemy pgvector type for ORM (optional; we use raw SQL for ANN queries but the type helps)

---

## Verification Plan

### Automated Tests
```bash
cd backend
pytest tests/unit/test_embedding_provider.py -v
pytest tests/unit/test_versioning.py -v
pytest tests/integration/test_embedding_pipeline.py -v
pytest tests/api/test_search.py -v
```

### Manual Verification
1. Run `alembic upgrade head` → confirm HNSW index exists in psql: `\d document_chunks`
2. Upload a PDF → wait for it to reach `READY` status → confirm all chunks have `embedding IS NOT NULL`
3. Call `POST /search` with a relevant query → verify results contain correct documents/pages
4. Confirm in logs that the HNSW index is being used (not a sequential scan)
5. Confirm tenant isolation: searching as Org A returns no Org B chunks

## Open Questions

> [!NOTE]
> The embedding model is already pinned in `config.py` as `text-embedding-3-small` with `embedding_dimensions=1536`, and the `vector(1536)` column was created in migration 007. This is consistent with the design doc's requirement to pin the model before the migration.

> [!IMPORTANT]
> The `openai` package is not yet in `requirements.txt` — it will be added. If you prefer to use a different embedding provider for Phase 7, update the `EMBEDDING_PROVIDER` env var — the abstraction supports swapping.

> [!NOTE]
> The INDEXING stage's "verify HNSW is used" check (`EXPLAIN ANALYZE`) will only reliably trigger the HNSW index at realistic data volumes. At dev/test scale with a handful of chunks, the planner may choose a sequential scan. This is logged as a warning, not an error — Phase 8 will have the production-scale validation.
