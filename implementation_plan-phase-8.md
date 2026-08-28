# Phase 8 — Hybrid Search and Reranking

## Overview

Phase 8 upgrades retrieval from vector-only (Phase 7) to production quality:

- **Full-text search branch** — PostgreSQL FTS over the `content_tsv` generated column, now backed by a GIN index, queried with parameter-bound `websearch_to_tsquery` (forgiving natural-language parsing; SQL-injection structural defense).
- **Reciprocal Rank Fusion** — vector top-50 + keyword top-50 (both within the mandatory permission/metadata scope) fused in Python with `score = Σ 1/(k + rank)`, `k = 60`.
- **Candidate selection** — top 20–30 fused candidates go to the reranker; final top 5–8 (bounded by `top_k`) are returned.
- **Cross-encoder reranking** — second AI provider behind its own abstraction; scores normalized to [0, 1]; candidates below the score threshold are dropped **even if within top-K by rank** (this is what makes "zero usable evidence" a reachable, honest outcome for Phase 9/10).
- **Graceful fallback** — reranker disabled/timeout/failure → unreranked fused ordering with a `warning` log; the request is degraded, never failed.
- **Metadata filtering** — `document_type`, `collection_id`, `department`, `owner_id` applied as additional `WHERE` clauses inside the SAME retrieval statement on both branches; temporal scope (`as_of`) resolved by reusing `resolve_current_version(as_of=…)` — the same domain function as "current" resolution.
- **Mode toggle** — `hybrid` (default) | `semantic` | `keyword` as one parameter that skips fusion branches (semantic skips FTS; keyword skips the vector branch AND reranking, since reranking's value is specifically in refining semantically-retrieved candidates).

The endpoint contract stays backward-compatible: every Phase 8 request field is optional, and the response gains `mode` + `used_reranker`.

## Proposed Changes

### Migration

#### [NEW] [`009_gin_fts_index.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/versions/009_gin_fts_index.py)

- GIN index `ix_document_chunks_content_tsv_gin` on the `content_tsv` generated tsvector (created in migration 007), built `CONCURRENTLY` (same pattern as migration 008's HNSW).
- Supporting relational-filter indexes per DB §27: `ix_documents_org_department (organization_id, department)` and `ix_document_versions_effective_date (document_id, effective_date)` for temporal resolution. Existing indexes already cover `document_type` (`ix_documents_org_type`) and `owner_id` (`ix_documents_owner_id`).

---

### Infrastructure Layer

#### [NEW] [`reranker.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/reranker.py)

The `RerankerProvider` abstraction (mirrors `embeddings.py`):
- `RerankerProvider.rerank(query, documents) -> list[float]` — one normalized [0,1] score per document, same order
- `CohereRerankerProvider` — hosted cross-encoder API; lazy client; ~5 s timeout; 1 retry; auth errors never retried; provider order mapped back to the caller's order
- `StubRerankerProvider` — deterministic lexical-overlap scores for tests/dev; never calls an API
- `normalize_scores` — clamps already-bounded scales, monotonically sigmoid-squashes unbounded ones (ordering always preserved)
- Process-global `get/set/init_reranker_provider`; `reranker_provider='none'` initializes to `None` = reranking disabled (configuration choice, not an error)

---

### Domain Layer

#### [MODIFY] [`versioning.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/domain/versioning.py)

`resolve_current_version(versions, as_of=None)` now supports point-in-time resolution (Backend §30 — same function, parameterized, never a separate historical path):
- Candidates = versions whose effective period covers `as_of` (`effective_date IS NULL OR <= as_of`; `expiration_date IS NULL OR > as_of`)
- Latest `effective_date` wins; undated versions rank lowest; `created_at` tie-break
- No candidate covers `as_of` → `None` (the document is excluded from that query — "didn't exist / wasn't effective then", not an error)

#### [NEW] [`search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/domain/search.py)

`SearchFilters` frozen dataclass — the metadata filter dimensions (`document_types`, `collection_ids`, `department`, `owner_id`), with `ALLOWED_DOCUMENT_TYPES` mirroring the DB CHECK constraint and a `sanitized()` helper that drops unknown types from mixed sets.

---

### Repository Layer

#### [MODIFY] [`document_chunk_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/document_chunk_repository.py)

- `keyword_search(organization_id, version_ids, query_text, top_k, filters)` — [NEW method]: FTS over `content_tsv @@ websearch_to_tsquery('english', :query_text)` ordered by `ts_rank`, with the identical mandatory predicate set as the vector branch (org + allowed versions + not-deleted) inside ONE statement.
- `semantic_search(..., filters=)` — metadata filters as additional WHERE clauses.
- `_metadata_filter_parts()` — shared builder producing filter fragments + bound params used identically by both branches (fixed SQL fragments only; every value is a bound parameter).

**Bug fixes surfaced by the new integration tests (latent Phase 7 defects):**
1. `text()` bind parsing: SQLAlchemy does NOT register `:param::type` casts — the bind name is silently truncated, so `:query_vec::vector`, `ANY(:version_ids::uuid[])` and `SET LOCAL hnsw.ef_search = :ef` could never have executed successfully against asyncpg. Replaced with `CAST(:param AS vector/uuid[])` and `SELECT set_config('hnsw.ef_search', :value, true)` (parameter-bound, transaction-scoped).
2. asyncpg rejects PostgreSQL `{...}` array literal strings for array parameters — native Python lists are now bound directly.

---

### RAG Layer

#### [NEW] [`hybrid_search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/hybrid_search.py)

- `rrf_fuse(vector_results, keyword_results, k=60)` — pure, deterministic Reciprocal Rank Fusion; records per-branch ranks; easily testable in isolation (Backend §31).
- `normalize_minmax` — presentation normalization for the unbounded `ts_rank`.
- `HybridRetriever.search(query, user, scope, filters, mode, top_k) -> HybridSearchOutcome`:
  1. Resolve the allowed version set ONCE — one scope resolution feeds both branches with identical predicates (Backend §29/§30); empty → short-circuit, never broadened
  2. Run the branch(es) per mode; hybrid fuses with RRF and selects the top `rerank_candidate_count`
  3. Rerank stage (below) → threshold + fallback
  4. Presentation relevance per Backend §39: reranker score → fused score (fallback) → normalized keyword score (keyword mode) → cosine (semantic mode without reranker); never raw cosine in a mixed result set

#### [NEW] [`reranker.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/reranker.py)

The rerank orchestration stage (Backend §32): calls the provider over the candidates' raw content, attaches normalized scores, applies `rerank_score_threshold` (drops even top-ranked marginal candidates), re-sorts, and on ANY provider failure returns the candidates unreranked in fused order with `used_reranker=False` — degraded, never failed.

`SearchResult` ([`retriever.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/retriever.py)) gains optional `rerank_score` / `fused_score` / `keyword_score` fields (Phase 7 constructors unaffected).

---

### Service Layer

#### [NEW] [`search_service.py`](file:///D:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/search_service.py)

`SearchService` — the thin orchestrator of Backend §39: normalizes the mode, delegates to `HybridRetriever`, logs degraded-mode signals. Deliberately stops before context assembly/LLM (Search and Chat share ~80% of the pipeline and diverge at the final stage; Phase 9 consumes this retriever).

#### [MODIFY] [`authorization_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/authorization_service.py)

`resolve_allowed_documents` honors `scope.as_of`: parses the ISO-8601 point-in-time and resolves each document's effective version through `resolve_current_version(as_of=…)`; documents with no version effective then are excluded (not an error).

---

### API Layer

#### [MODIFY] [`search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/search.py)

`POST /search` now accepts:
- `mode: hybrid|semantic|keyword` (default from settings)
- `filters: { document_types?, collection_ids?, department?, owner_id? }` — unknown document types rejected with 422
- `scope.as_of` — temporal point-in-time

Response adds `mode` and `used_reranker`, and surfaces a degraded-mode notice in `message` when the reranker fell back in hybrid/semantic mode. The Phase 7 shadowed duplicate of `GET /documents/{id}/chunks` was removed (the richer documents-router version is the only registered route) and that route's path param is now typed `UUID` so invalid IDs return 422 instead of a 500 from the DB layer.

---

### Config

#### [MODIFY] [`config.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/core/config.py)

Phase 8 settings (all tunable — Backend §31: these get adjusted by Phase 18's evidence, never hardcoded):
- `reranker_provider: cohere | stub | none` (default `none`), `reranker_model`, `reranker_timeout_seconds=5.0`, `reranker_max_retries=1`
- `rerank_score_threshold=0.35` — initial value in the documented 0.3–0.4 band; the risk-note requirement to "record the initial value" is satisfied here
- `search_mode_default=hybrid`, `hybrid_vector_top_k=50`, `hybrid_keyword_top_k=50`, `rerank_candidate_count=30`, `rrf_k=60`

#### [MODIFY] [`main.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/main.py)

Lifespan initializes the reranker provider after the embedding provider; failure is non-fatal (degraded ordering, never a blocked application).

#### [MODIFY] [`.env.example`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/.env.example)

All Phase 8 environment variables documented with their defaults.

---

### Requirements

#### [MODIFY] [`requirements.txt`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/requirements.txt)

- `cohere==5.13.4` — lazy-imported; only required when `reranker_provider='cohere'` (`none`/`stub` never touch the SDK)

---

### Tests

#### [NEW] [`tests/unit/test_hybrid_search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_hybrid_search.py)

- RRF math: known rankings → known fused order; fused scores match the formula; k-dampening (a both-branch chunk beats a single-branch #1); disjoint unions
- Threshold filtering: below-threshold candidates dropped even when top-ranked; all-below-threshold → honest empty set
- Reranker fallback: provider None/error/mismatch → fused order preserved, never raises
- Mode-toggle branch skipping: keyword never hits the vector branch nor reranks; semantic never hits FTS; hybrid runs both; empty scope short-circuits every branch
- `SearchFilters` sanitization

#### [NEW] [`tests/unit/test_reranker_provider.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_reranker_provider.py)

Stub determinism/overlap ordering, score normalization, Cohere construction guards (missing SDK → typed error, missing key → ValueError), provider selection via settings, process-global get/set contract.

#### [MODIFY] [`tests/unit/test_versioning.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_versioning.py)

Temporal `as_of` resolution: effective-at-time selection, not-yet-effective/expired exclusion, boundary inclusivity, undated handling, ISO-string acceptance, unparseable-input safety.

#### [NEW] [`tests/integration/test_hybrid_search_pipeline.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_hybrid_search_pipeline.py)

(testcontainers; the roadmap's Phase 8 §Testing items)
- Phase 8 indexes exist; FTS plan provably uses the GIN index (`enable_seqscan=off` forcing)
- Keyword branch: matching/ranking over a seeded corpus, exact-identifier match (`4.2 escalation`)
- Isolation matrix on the keyword branch: org boundary, allowed-version boundary, soft-delete exclusion
- `websearch_to_tsquery` is forgiving: hostile input returns empty results, never a 500
- Metadata filters combine with AND inside both branch queries (type / department / owner / collection)
- HybridRetriever end-to-end over real SQL: ranked results with rerank; simulated reranker outage → unreranked fused results (degraded, not failed)
- Temporal exit criterion: `as_of` returns the correct historical version via the REAL `AuthorizationService`

#### [MODIFY] [`tests/api/test_search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/api/test_search.py)

Mode-toggle contract (defaults, per-mode echo, invalid mode → 422), filter validation, temporal scope acceptance, response shape with `mode`/`used_reranker`. The registration helper was repaired (field names now match the auth schema — it had never actually passed).

#### [MODIFY] harness files

- `tests/conftest.py` — `db_session` no longer wraps tests in a SAVEPOINT (any test-internal commit released it and made teardown fail); cleanup tables extended to the document domain in FK-safe order
- `tests/integration/test_embedding_pipeline.py` — repaired pre-existing broken seeds (missing user row → `documents_owner_id_fkey` violation; wrong `document_pages` column name; `:v::vector` cast pattern); ORM mapper registration imports

All three-mode correctness runs are recorded by the tests above; the standing quality-comparison harness (vector-only vs hybrid vs hybrid+rerank metrics) is formalized in Phase 18 using this same `POST /search` endpoint, as the roadmap specifies.

## Verification Plan

### Automated Tests

```bash
cd backend
pytest tests/unit/test_hybrid_search.py tests/unit/test_reranker_provider.py tests/unit/test_versioning.py -v
pytest tests/integration/test_hybrid_search_pipeline.py -v   # requires Docker
pytest tests/api/test_search.py -v                           # requires Docker
pytest tests                                                 # full suite: 351 passed, 1 skipped
```

### Manual Verification

1. `alembic upgrade head` → confirm in psql: `\d document_chunks` shows the GIN index; `\d documents` shows `ix_documents_org_department`
2. `POST /search {"query": "..."}` → `mode: "hybrid"`; with `RERANKER_PROVIDER=none` the response carries `used_reranker: false` and a degraded note
3. `POST /search {"mode": "keyword"}` → keyword results, never reranked by design
4. `POST /search {"scope": {"as_of": "2025-01-01"}}` → the version effective at that date is searched
5. Filter combination: `{"filters": {"document_types": ["policy"], "department": "Legal"}}` behaves as AND
6. Set `RERANKER_PROVIDER=cohere` + `COHERE_API_KEY` → hybrid responses show `used_reranker: true`

## Open Questions

> [!IMPORTANT]
> The `cohere` package is in `requirements.txt` but NOT yet installed in the checked-in virtualenv. The lazy-import design means everything runs without it (`reranker_provider='none'` default); run `pip install -r requirements.txt` before enabling `RERANKER_PROVIDER=cohere`.

> [!NOTE]
> `rerank_score_threshold=0.35` is the deliberately recorded starting value (midpoint of the spec's 0.3–0.4 band). Phase 18's evaluation loop tunes it; changing it is a config edit, never a code change.

> [!NOTE]
> Phase 7's integration tests had never successfully executed (their seeds violated `documents_owner_id_fkey` before any assertion ran), which is why the repository-level `::type` cast and array-literal bugs survived Phase 7's exit review. The Phase 8 integration suite now exercises the real SQL paths, and the whole suite is green (351 passed / 1 skipped).
