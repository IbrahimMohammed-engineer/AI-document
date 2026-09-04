# Phase 12 — Document Versioning and Comparison
## Implementation Plan

**Audience:** an implementing coding agent (GLM 5.3 Flash) with no prior context on this codebase beyond this document and the four source architecture documents. This plan makes every architectural decision explicit. Where the source documents conflict or are silent, this plan states the resolution and marks it as a plan-authored decision, not a quotation.

**Prepared by:** analysis of the actual repository state (backend, database, frontend, tests) cross-referenced against `Documentation/AI-Document-Intelligence-Platform-Implementation-Roadmap.md`, `Documentation/Backend-Architecture-Documentation.md`, `Documentation/Database-Architecture-Design-Documentation.md`, `Documentation/Frontend-Design-Documentation.md`, and the prior `implementation_plan-phase-*.md` files, as of the current repository state (Alembic head = revision `011`).

---

## 1. Executive Summary

The codebase already has a **solid, mostly-complete versioning foundation** (Phases 3–11): `documents`/`document_versions` with `version_number`, `effective_date`/`expiration_date`, a `current_version_id` pointer, a full processing-pipeline status state machine, and a `GET /documents/{id}/versions` endpoint. It also has **deliberate forward-compatible scaffolding for Phase 12** left in place by earlier phases: `JobType.COMPARISON` already exists (DB CHECK constraint + retry policy), `PermissionKey.COMPARISON_CREATE = "comparison:create"` is already seeded to Admin/Editor roles, the query analyzer already classifies `COMPARISON`/`CHANGE_DETECTION` intents (currently logged and silently routed to generic RAG), and `document_chunks.content_hash` exists specifically for comparison short-circuiting.

**What does not exist at all:** the `document_comparisons`/`comparison_changes` tables, any comparison service/worker/API, any comparison UI beyond two placeholder routes, and (found during this investigation, not anticipated by any source document) the entire `frontend/src/lib` API-client layer that the rest of the frontend already assumes exists.

**One verified bug in existing code directly blocks correct Phase 12 behavior:** `domain/versioning.py::resolve_current_version()` (the pure function that is supposed to determine "the current version") references a `is_current` flag that **does not exist** on the `DocumentVersion` model (confirmed: no such column, no such SQLAlchemy attribute). Since `getattr(v, "is_current", False)` is always `False`, the function's "current" (no `as_of`) branch **always** falls back to "latest by `created_at`" — ignoring `effective_date`/`expiration_date` entirely. This means a future-dated ("scheduled") version can already be surfacing as "current" today, contradicting the documented business rule. This must be fixed before Phase 12's version-state classification and comparison-default-resolution logic can be correct (§8.4, §11 Task 3).

This plan scopes Phase 12 to exactly the vertical slice the roadmap defines (M6: *Version A + B → comparison → classified, source-linked changes*), reuses every existing mechanism it can (state machines, worker/queue infra, citation/provenance patterns, provider abstractions, RBAC), and makes concrete, previously-undocumented decisions where the four source documents disagree or are silent (endpoint naming, `processing_jobs` schema for a two-version job, severity thresholds, chat-intent version resolution, cross-document comparison ordering).

---

## 2. Phase Objective

Per the roadmap (lines ~1930–1932, quoted verbatim):

> "Complete versioning semantics (effective dates, current-version resolution as a first-class behavior, historical querying) and build the comparison pipeline: section alignment, text + semantic comparison, change detection and classification (ADDED/REMOVED/MODIFIED + MAJOR/MODERATE/MINOR), citation mapping to old/new chunks, persisted reusable results, and the CHANGE_DETECTION conversational surface — vertical slice **M6**."

Exit criterion (roadmap, verbatim): "The 2025-vs-2026 fixture comparison detects the approval-window change as MODIFIED with correct severity, word-level diff, and View Sources navigating into both versions' exact pages; re-requesting the same pair returns the persisted result without recomputation; historical-scope questions ('in 2025') retrieve the correct version's chunks; version-status and effective-date matrices pass unit tests."

---

## 3. Source Documents

- `Documentation/AI-Document-Intelligence-Platform-Implementation-Roadmap.md` — Phase 12 section (~lines 1928–2055), Phase 13/14 sections (scope boundaries), Phase 9–11 sections (what already exists).
- `Documentation/Backend-Architecture-Documentation.md` — §16 (versioning), §27 (query intent), §40 (comparison pipeline), §41 (change detection), §42 (conflict detection — boundary only), §45/§48 (API conventions), §47 (state machines).
- `Documentation/Database-Architecture-Design-Documentation.md` — §13–16 (documents/versions/pages/sections/chunks), §25 (`document_comparisons`/`comparison_changes` — the primary new schema), §28 (constraints/cascades), §29 (soft-delete conventions).
- `Documentation/Frontend-Design-Documentation.md` — §6.5 (Document Workspace), §6.10–6.11 (Comparison UI, ChangeCard), §9–10 (API/React Query conventions), §12–13 (citation navigation, viewer).
- Prior phase plans (`implementation_plan-phase-9.md`, `-10.md`, `-11.md`) — for file/task-format conventions only.
- **Actual repository state** — verified directly by reading code (not inferred from docs) for every decision in §4 and every file path in §17.

---

## 4. Current Implementation Assessment

### 4.1 Already Implemented (reuse as-is)

| Item | Location | Notes |
|---|---|---|
| `documents` table + model | `backend/app/models/document.py:57-176`, migration `004` | `current_version_id` FK (nullable, `ON DELETE SET NULL`), `access_level`, soft-delete via `deleted_at`. No changes needed. |
| `document_versions` table + model | `document.py:181-285`, migration `004` | `version_number` (app-assigned, unique per document), `effective_date`/`expiration_date` (nullable `Date`), full 9-state status enum **including `CHUNKING`** (the Backend-vs-Database doc discrepancy the roadmap itself flagged at Phase 3 is already resolved in code — verified: `ck_document_versions_status` includes `CHUNKING`). No schema changes needed. |
| `document_chunks.content_hash` | `document.py:595-599`, migration `007` | `String(64)` NOT NULL, SHA-256 hex, comment explicitly says "Phase 12 cheap change pre-diff". Ready to consume as-is. |
| `JobType.COMPARISON` | `app/domain/state_machines.py:43` | Already in enum, already has a retry-policy entry (`3` attempts, comment "may call LLM providers (Phase 12)"), already in the `processing_jobs.job_type` DB CHECK constraint. No enum change needed. |
| `PermissionKey.COMPARISON_CREATE` | `app/domain/permissions.py:21` (`"comparison:create"`) | Already seeded to Admin/Editor roles (migration `002`, confirmed by `tests/unit/test_permissions.py`). Reuse directly — do not invent a new permission key. |
| `GET /documents/{id}/versions` | `app/api/documents.py:321-335` → `DocumentService.get_version_list` | Already returns `list[DocumentVersionDetail]` (schema in `app/schemas/document.py:60-64`). Needs enrichment (§4.4), not creation. |
| `GET /documents/{id}/download?version=` | `app/api/documents.py:340-359` | Already version-parameterized (optional `version: int` query param). Precedent for version-scoped endpoints. |
| Query intent classification | `app/rag/query_analyzer.py:50-67` | `QueryIntent` Literal and `DEFERRED_INTENTS` already include `COMPARISON`/`CHANGE_DETECTION`. `analyze_query()` already classifies them; currently just logs and falls through to standard RAG (`query_analyzer.py:231-239`). Needs routing, not classification. |
| Citation/provenance pattern | `app/models/message.py:153-255`, `app/rag/citations.py`, `app/rag/citation_validator.py` | Denormalized, chunk-FK-anchored, RESTRICT-on-delete. This is the pattern comparison provenance mirrors (not reuses directly — see §4.5 Gap 3). |
| `LLMProvider`/`EmbeddingProvider` abstractions | `app/infrastructure/llm.py`, `app/infrastructure/embeddings.py` | Single `generate()`/`embed()` methods, `StubLLMProvider`/`StubEmbeddingProvider` for tests. Reuse directly for semantic comparison, section-title-embedding fallback, and change narration. |
| Worker/queue infrastructure | `app/workers/jobs.py`, `app/infrastructure/queue.py` | Arq-based; `HANDLERS: dict[JobType, JobHandler]` registry; `run_processing_job()` single entry point; `DeterministicJobError`/`RetryableJobError`; per-job-type retry via `state_machines.get_max_attempts()`. Reuse the pattern; requires one dispatch-branch modification (§4.4). |
| State machines | `app/domain/state_machines.py` | `JobStatus`/`VersionStatus` transitions are formally enforced (not ad hoc). Comparison jobs use the existing `JobStatus` machine unmodified. |
| Exception hierarchy | `app/core/exceptions.py` | `NotFoundError`(404), `ValidationError`(422), `ForbiddenError`/`InsufficientPermissionsError`(403), `ConflictError`(409), `ExternalServiceError`(503). Reuse directly — no new exception subclasses needed. |
| `organizations.settings` JSONB | `app/models/organization.py:57-63` | Nullable-free JSONB, default `{}`. Reuse for a new `settings["comparison"]["critical_sections"]` key (§9.8) — no migration needed for this. |
| Test infra conventions | `backend/tests/conftest.py` | testcontainers Postgres+Redis, hand-written seeding helpers via real repos (no factory_boy despite it being installed), `StubLLMProvider(responder=...)`/`StubEmbeddingProvider(dimensions=...)` injection pattern, `_CLEANUP_TABLES` list. Reuse the pattern; requires additions (§16). |

### 4.2 Partially Implemented (exists but incomplete/incorrect for Phase 12)

1. **`resolve_current_version()`** (`app/domain/versioning.py:63-140`) — the `as_of`-supplied branch is correct and well-tested (`tests/unit/test_versioning.py`). The **no-`as_of` branch is broken**: it looks for `is_current=True` (an attribute that does not exist anywhere in the schema or ORM model) and — because that lookup always returns nothing — silently degrades to "latest by `created_at`" for *every* call with no `as_of`, which is most calls in the codebase today (`authorization_service.py:210`, `workers/jobs.py:388`). This ignores `effective_date`/`expiration_date` and can select a future-dated ("scheduled") version as current. **Must fix — see §8.4 and Task 3.**
2. **`GET /documents/{id}/versions`** — exists and returns real data, but does not expose the roadmap-required "current / superseded / scheduled" classification (Backend §47's documented-but-not-stored business classification). Frontend has nothing to render a version history/selector against it either.
3. **Effective-date validation** — `document_service.py:314-319` currently parses `effective_date` from the upload form and **silently ignores unparseable dates** ("validation is at API schema level" — but no `expiration_date >= effective_date` check exists anywhere in the code read for this plan). The roadmap's Phase 12 step 1 explicitly requires this validation with a 422 response. **Must add.**
4. **`ProcessingJob` model** — fully functional for single-version jobs, but `document_version_id` is a single `NOT NULL` FK (`app/models/processing_job.py:88-92`). A comparison job inherently needs two version references. **Must extend (not replace) the schema — see §10, Task 1.**
5. **Authorization** — `AuthorizationService.resolve_allowed_documents()` (`authorization_service.py:70-224`) is a bulk, org-wide scope resolver, not a single-document/version authorizer. There is no existing helper of the shape "can this user access version X specifically" that a comparison endpoint can call twice (once per side). **Must add a new method that factors out the existing per-document `access_level` check (lines 158-176) so both code paths share one implementation — see Task 4.**

### 4.3 Missing (build from scratch)

- `document_comparisons` and `comparison_changes` tables/models/migration — confirmed absent (no migration file, no model class, zero hits searching `backend/app` and `backend/alembic` for these names beyond the `COMPARISON`/`comparison:create` placeholders already covered in §4.1).
- `ComparisonService`, comparison repository, comparison worker handler, section-alignment logic, text-comparison logic, semantic-comparison LLM call, `domain/comparison_rules.py` (severity), citation-mapping-for-comparisons logic — none exist.
- `POST /compare`, `GET /compare/{id}`, `GET /compare/{id}/changes`, `GET /compare/{id}/changes/{changeId}` endpoints and their Pydantic schemas — none exist.
- Chat/`ask` routing for `COMPARISON`/`CHANGE_DETECTION` intents to a dedicated service — the analyzer classifies these intents today but nothing downstream branches on them (`app/services/ask_service.py:268` always proceeds through the same RAG path regardless of `analysis.intent`).
- All comparison-related tests (unit/integration/API) — zero exist anywhere in `backend/tests`.
- Frontend comparison UI (`ComparisonSelector`, `ComparisonSummary`, `DiffViewer`, `SectionNavigator`, `ChangeCard`, `ChangeSeverityBadge`) — the `/app/compare` and `/app/compare/:comparisonId` routes exist in `frontend/src/App.tsx:198-199` but both render the generic `PlaceholderPage` stub with zero logic.
- Frontend version history/selector UI in the Document Workspace — does not exist; `DocumentWorkspace.tsx` renders only `document.current_version` as static text, with no way to view or select another version.
- **`frontend/src/lib/` (the entire API client + React Query client + SSE-realtime layer)** — verified absent via direct filesystem search (not gitignored, not a false negative). Every existing frontend feature file (`useDocuments.ts`, `useDocumentProcessing.ts`, `useConversations.ts`, `App.tsx`, `authStore.ts`, and the `ask`/`documents` feature components) imports from `@/lib/api/*`, `@/lib/query/client`, `@/lib/realtime` — none of which resolve to anything on disk. **The frontend cannot build as currently checked out, independent of Phase 12.** This is flagged as `UNKNOWN / REQUIRES DECISION` in §4.5 Gap 6 and handled as a blocking prerequisite task, not silently worked around.

### 4.4 Required Modifications (existing code that must change)

1. `app/domain/versioning.py::resolve_current_version()` — remove the `is_current`-flag lookup in the no-`as_of` branch; make "current" behave as `as_of=today` (apply the same effective/expiration-window filtering the `as_of` branch already implements). See §8.4, Task 3.
2. `app/models/processing_job.py` / migration — add a nullable `comparison_id` FK column + index + a CHECK constraint tying `job_type='COMPARISON'` to `comparison_id IS NOT NULL`. See §10, Task 1.
3. `app/workers/jobs.py::run_processing_job()` — add an early branch: when `job.job_type == JobType.COMPARISON`, skip the existing "load version + document, transition `VersionStatus`" logic (lines ~466-519, which does not apply to a comparison job) and instead load the `DocumentComparison` via `job.comparison_id`, then dispatch to a new `handle_comparison` handler with a different signature. See §12, Task 8.
4. `app/services/authorization_service.py` — extract the per-document `access_level` check (currently inline at lines 158-176 inside `resolve_allowed_documents`) into a standalone function, then add `authorize_document_version()` built on top of it. See §9.2, Task 4.
5. `app/services/document_service.py` (upload / new-version path, ~lines 298-326) — call the new `validate_effective_window()` domain function before persisting `effective_date`/`expiration_date`, raising `ValidationError` (422) instead of silently ignoring bad input.
6. `app/services/ask_service.py` (and, since `ChatService` delegates to it, `app/services/chat_service.py:402`) — add branching on `analysis.intent in ("COMPARISON", "CHANGE_DETECTION")` to call the new comparison-narration path instead of falling through to generic RAG. See §14, Task 11.
7. `backend/alembic/env.py` — add the new `DocumentComparison`/`ComparisonChange` model imports to the explicit model-import list (autogenerate silently ignores unimported models in this codebase's setup).
8. `backend/tests/conftest.py::_CLEANUP_TABLES` — add `"comparison_changes"` and `"document_comparisons"` in FK-safe order (before `document_versions`/`documents`, after nothing else references them).
9. Frontend: `frontend/src/features/documents/DocumentWorkspace.tsx` — currently reads `page`/`q` search params for citation navigation but ignores an already-sent `version` param (`CitationBadge.tsx`/`CitationList.tsx` already set `?version=` when navigating, but the workspace never reads it). Wire it up as part of adding version-awareness (§15, Task 14).

### 4.5 Architecture/Implementation Gaps (explicit, not silently resolved)

**Gap 1 — Table naming: `comparisons` (task framing) vs. `document_comparisons` (Database Architecture doc §25).**
Expected: the Database Architecture document names the tables `document_comparisons` and `comparison_changes` explicitly, with full column definitions.
Current: no such tables exist yet, so there is no code-level naming to conflict with.
Gap: this plan's own instructions colloquially say "comparisons"/"comparison_changes".
**Recommended resolution:** use the Database Architecture doc's exact names — **`document_comparisons`** and **`comparison_changes`** — for consistency with the existing `documents`/`document_versions`/`document_pages`/`document_sections`/`document_chunks` naming family. This plan uses these names throughout.

**Gap 2 — Endpoint path and request-body shape disagreement between Backend/Roadmap and Frontend docs.**
Expected (Backend §45, Roadmap): `POST /compare`, body `{ documentAVersionId, documentBVersionId }`; detail at `GET /compare/{id}`.
Expected (Frontend §6.10, §10.2): `POST /comparison`, body `{ documentAId, versionA, documentBId, versionB }`; detail at `GET /comparison/:id` (while the *page* route in the same document's site map is `/compare/:comparisonId`, an internal inconsistency in the Frontend doc itself).
Gap: three of the four source documents disagree with each other on both the URL segment and the field names.
**Recommended resolution:** the backend is the single source of truth in this modular monolith; use **`POST /compare`** with body **`{"document_a_version_id": "...", "document_b_version_id": "..."}`** (snake_case, matching every existing schema in `app/schemas/*.py`, e.g. `DocumentUploadResponse.version_id`). Detail/list routes: `GET /compare/{comparison_id}`, `GET /compare/{comparison_id}/changes`, `GET /compare/{comparison_id}/changes/{change_id}`. The frontend page route stays `/app/compare`/`/app/compare/:comparisonId` (already reserved in `App.tsx`) and its API-client functions target the backend's actual contract, not the Frontend doc's example JSON.

**Gap 3 — `processing_jobs` cannot represent a two-version job as currently modeled.**
Expected (Backend §40, Roadmap "Infrastructure Work"): comparison "flows through the same `processing_jobs`/worker infrastructure" as ingestion jobs.
Current: `processing_jobs.document_version_id` is a single `NOT NULL` FK (verified in `app/models/processing_job.py:88-92`); no column exists for a second version, and neither architecture document specifies how this should be resolved (the Database doc has no dedicated `processing_jobs` schema section at all — a documentation gap independently noted during research).
**Recommended resolution:** add a nullable `comparison_id` FK (→ `document_comparisons.id`, `ON DELETE CASCADE`) to `processing_jobs`. Keep `document_version_id` `NOT NULL` for schema/index stability, but for `job_type='COMPARISON'` rows populate it with `document_a_version_id` (documented in a column comment as "anchor version for indexing/bookkeeping — see `comparison_id` for the full pair"). Add `CHECK ((job_type = 'COMPARISON') = (comparison_id IS NOT NULL))`. Full detail in §10, Task 1.

**Gap 4 — Cross-document comparison ordering for the unique-pair constraint.**
Expected (Database Architecture doc §25): "order-normalized at the application layer, e.g., always storing the lower `version_number`'s version as 'A'".
Current: N/A (table doesn't exist yet).
Gap: `version_number` is only meaningful *within* a document (it is not a global ordering key), yet the roadmap's frontend section explicitly allows cross-document comparisons ("Comparison experience... same-document version pairs pre-filtered; **cross-document**"). "Lower `version_number`" is ambiguous/meaningless when A and B belong to different documents.
**Recommended resolution:** normalize by comparing the tuple `(document_id, version_number)` lexicographically (string comparison on `document_id`, then integer comparison on `version_number`) — deterministic regardless of whether the two versions share a document. Always store the lexicographically smaller tuple's version as `document_a_version_id`. Documented fully in §9.3, Task 6.

**Gap 5 — Severity (MAJOR/MODERATE/MINOR) thresholds are described qualitatively, never quantitatively.**
Expected (Backend §40): severity is "a rule-informed-by-semantics classification... a pure, unit-testable function" with three qualitative inputs (semantic materiality, org-configured critical-section categories, proportion of section changed) — no document gives exact thresholds or a combination formula.
**Recommended resolution:** this plan proposes a concrete, first-implementable threshold set (§9.8, §13) explicitly labeled as plan-authored (not extracted from any document) so it is not mistaken for a documented requirement. It is a pure function (`domain/comparison_rules.py`), easy to retune later without touching the pipeline around it.

**Gap 6 — `frontend/src/lib` does not exist (verified, not anticipated by any source document).**
Expected: none of the four documents anticipate this — they all assume a working, buildable frontend exists as the starting point for Phase 12 frontend work.
Current: confirmed absent via direct filesystem search (`Glob frontend/src/lib/**` → no files; not listed in `.gitignore`; no `dist/`, no build artifacts present either). This repository is also **not under git** (per the environment's own report), so there is no version history to recover this layer from within the working copy.
**`UNKNOWN / REQUIRES DECISION`:** before rebuilding this layer from scratch, confirm with the team whether a canonical copy of `frontend/src/lib` exists elsewhere (a different branch, a backup, a teammate's working copy) — reconstructing it purely from call-site inference (as this plan's Task 13 does, as a fallback) risks diverging from whatever the "real" implementation actually did (auth-refresh-interceptor details, SSE reconnection specifics, etc.), even though the reconstruction is functionally sufficient to unblock Phase 12. If no canonical copy exists, proceed with Task 13 as specified.

**Gap 7 — `conflicts.detection_method` includes `retrieval_time`, with no clear write path; and the 5-vs-6-intent set (Backend §27 vs. Roadmap Phase 14) — neither blocks Phase 12.**
Noted for completeness (full detail was surfaced during research) but explicitly **out of scope**: Phase 12 only needs `COMPARISON`/`CHANGE_DETECTION` intents live; `SUMMARY`/`CONFLICT_DETECTION`/`EXTRACTION` remain classified-but-deferred exactly as they are today (`query_analyzer.py`'s existing fallback-to-QUESTION behavior is preserved for those three). Do not touch this in Phase 12.

---

## 5. Phase 12 Scope

**In scope** (the M6 vertical slice):
- Fix `resolve_current_version()`; add version-state classification (CURRENT/SUPERSEDED/SCHEDULED); add effective-date validation.
- `document_comparisons` + `comparison_changes` migration.
- `ComparisonService`: authorization (both sides), reuse-check, creation, orchestration.
- Comparison worker pipeline: section alignment → text comparison (content-hash short-circuit) → semantic comparison (bounded LLM call) → change classification → severity → citation/source mapping → incremental persistence.
- `POST /compare`, `GET /compare/{id}`, `GET /compare/{id}/changes`, `GET /compare/{id}/changes/{changeId}`.
- `CHANGE_DETECTION`/`COMPARISON` chat-intent routing to the comparison service + constrained narration.
- Frontend: reconstruct the missing `lib/` client layer (blocking prerequisite); version selector/history in Document Workspace; comparison picker + results UI (`DiffViewer`, `SectionNavigator`, `ChangeCard`, severity badges, summary card); wire the already-sent `version` citation param.
- Full test suite: unit (rules, alignment, classification, reuse), integration (pipeline, worker retry/resume), API (authz, status codes, idempotency), one end-to-end fixture (2025 vs. 2026 Marketing Policy).

**Explicitly out of scope** (do not build in Phase 12):
- The full Documents list page (`/app/documents` placeholder) — that is Phase 3's unfinished responsibility per its own placeholder copy ("Document library — Phase 3"), unrelated to Phase 12. Build only a lightweight comparison document/version picker, not the full sortable/filterable table.
- `conflicts`/`conflict_statements` tables, the background conflict-scan worker, the conflict resolution workflow, and TOC `[•]` conflict markers — all Phase 13.
- `SummaryService`, and routing for `SUMMARY`/`CONFLICT_DETECTION`/`EXTRACTION` intents — Phase 14.
- Any new infrastructure component (message bus, dedicated vector DB, microservice, GraphQL, Kubernetes, Elasticsearch). Comparison is a service + worker inside the existing modular monolith, using the existing Postgres/Redis/Arq stack.
- PDF-pixel-level highlight/navigation (bounding-box overlays on the actual rendered PDF) — the existing citation-navigation mechanism (URL params into an extracted-text side panel) is the reuse target; do not build a PDF.js text-layer/bounding-box system as part of Phase 12 (no such library is even installed, and neither the Backend nor Roadmap docs make this a Phase 12 requirement — Frontend §13 describes it as the general Document Viewer target, not gated to Phase 12).

---

## 6. Dependencies

**Depends on:** Phase 11 (chat/streaming — done; `ChatService`/`AskService`/conversations exist and are the integration point for §14). Version data has existed since Phase 3.

**Blocks:** Phase 13 (conflict detection reuses comparison machinery — this plan adds a no-op extension point but does not implement seeding), Phase 14 (summarization, remaining intent routing), Phase 15 (comparison UI consolidation).

**Parallelizable within this plan:** the frontend `DiffViewer`/`ComparisonResults` UI (Task 15) can be built against a mocked/stubbed API response in parallel with the backend comparison pipeline (Tasks 5–9), since the API contract (§13) is fixed early. The `lib/` client reconstruction (Task 13) should happen first, though, since every other frontend task imports from it.

---

## 7. Target Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              POST /compare                │
                    │  {document_a_version_id, document_b_version_id} │
                    └───────────────────┬───────────────────────┘
                                        │
                    Authorize BOTH sides (AuthorizationService.authorize_document_version × 2)
                                        │
                    Normalize pair order — (document_id, version_number) tuple compare
                                        │
                    Reuse check — SELECT document_comparisons WHERE (org, a, b) UNIQUE
                                        │
                          found? ──yes──► return existing row (200 or current status)
                                │no
                                ▼
                    INSERT document_comparisons (status=PENDING)
                                │
                    INSERT processing_jobs (job_type=COMPARISON, comparison_id=...)
                                │
                    commit ─► enqueue Arq pointer (202 Accepted, comparisonId)
                                │
                    ═══════════ Comparison Worker (handle_comparison) ═══════════
                                │
                    Section Alignment (section_number match → title-embedding fallback)
                                │
                    Text Comparison (content_hash short-circuit → normalized-text diff)
                                │
                    Semantic Comparison (bounded LLMProvider.generate() call per MODIFIED candidate)
                                │
                    Change Classification (ADDED/REMOVED/MODIFIED; UNCHANGED never persisted)
                                │
                    Severity (domain/comparison_rules.py — pure function)
                                │
                    Citation/Source Mapping (old_chunk_id/new_chunk_id + denormalized text snapshots)
                                │
                    Persist comparison_changes incrementally (per section, resumable)
                                │
                    document_comparisons.status = COMPLETED, summary = {...}
                    ════════════════════════════════════════════════════════════
                                │
        GET /compare/{id}  ·  GET /compare/{id}/changes?severity=&section=
                                │
        ┌───────────────────────┴────────────────────────┐
        │                                                  │
  Comparison UI (DiffViewer,                    Chat CHANGE_DETECTION/COMPARISON intent
  ChangeCard, SectionNavigator)                  → ComparisonService.get_or_create_comparison()
                                                  → constrained narration LLM call (phrasing only)
                                                  → persisted as a normal assistant Message + Citations
```

---

## 8. Versioning Semantics

### 8.1 Version Creation

Unchanged from current implementation — no modification needed here beyond §8.6's validation addition:
- `version_number` is computed as `(MAX(version_number) for this document_id) + 1` inside the same DB transaction that inserts the new `DocumentVersion` row (`document_service.py:298-300`).
- **Never client-supplied.**
- Concurrency backstop: the existing `UniqueConstraint("document_id", "version_number")` — no explicit row lock (`SELECT ... FOR UPDATE`) is taken today. A genuine race (two concurrent uploads of the same document) can raise an `IntegrityError` on the unique constraint; the existing code does not currently catch-and-retry this. **This is a pre-existing gap, not a Phase 12 requirement to fix** (the roadmap does not call it out for Phase 12) — note it in §20 Risks but do not add retry logic unless the team explicitly asks; it is out of this plan's scope to touch upload concurrency handling.
- First version of a new document: `Document` row is created and flushed first, then the version resolves to `version_number=1`, and `current_version_id` is set to it immediately in the same transaction (`document_service.py:323-326`) — while the version is still `UPLOADED`, not yet `READY`. This immediate pointer-set only happens for the very first version; later versions get `current_version_id` updated only by the worker's `handle_indexing` step once `READY` (`workers/jobs.py:380-399`). This asymmetry is intentional (a document must always have *some* pointer, even mid-processing) and is preserved as-is.

### 8.2 Version Numbering

No change. `version_number: Integer NOT NULL`, unique per `(document_id, version_number)`, immutable once assigned (no code path renumbers an existing version).

### 8.3 Version States

`document_versions.status` (unchanged, already correct): `UPLOADED → PROCESSING → EXTRACTING → OCR → CHUNKING → EMBEDDING → INDEXING → READY`, with `FAILED` reachable from any non-terminal state and terminal; `FAILED → PROCESSING` only via the explicit retry endpoint (`POST /documents/{id}/retry`). Enforced by `domain/state_machines.py::assert_version_transition()`. **No changes in Phase 12.**

### 8.4 Current Version Resolution — REQUIRED FIX

**Problem (verified):** `resolve_current_version(versions, as_of=None)` (`app/domain/versioning.py:63-140`):
```python
# Prefer the explicitly flagged current version
flagged = [v for v in versions if getattr(v, "is_current", False)]   # ALWAYS empty — no such attribute exists
if flagged:
    return max(flagged, key=_created_at_of)
# Fallback: latest by creation time
return max(versions, key=_created_at_of)   # <-- this ALWAYS runs today
```
Every caller that invokes this function without `as_of` (`authorization_service.py:210`, `workers/jobs.py:388`) therefore always gets "latest `READY` version by `created_at`" — **not** "latest version whose `effective_date` has arrived and hasn't expired." A version uploaded today with `effective_date` six months in the future would become "current" the moment it reaches `READY`, contradicting the documented rule (Backend §16 point 4 / roadmap Phase 12 step 1: "future-dated versions stay 'scheduled'").

**Fix:** remove the `is_current` lookup entirely (per the Database Architecture doc's own explicit design rationale: *"rather than an `is_current` boolean on `document_versions`, which would require a database trigger... a classic source of data-integrity bugs"* — the doc-set never intended this flag to exist, so the domain code referencing it is the actual bug, not a missing column to add). Make the no-`as_of` path behave **identically** to `as_of=today`:

```python
def resolve_current_version(versions: list, as_of: date | datetime | None = None) -> object | None:
    if not versions:
        return None
    as_of_date = _to_date(as_of) if as_of is not None else date.today()
    if as_of is not None and as_of_date is None:
        as_of_date = date.max   # unparseable as_of degrades to "current" behavior — unchanged
    candidates = []
    for v in versions:
        eff = _to_date(getattr(v, "effective_date", None))
        exp = _to_date(getattr(v, "expiration_date", None))
        if eff is not None and eff > as_of_date:
            continue
        if exp is not None and exp <= as_of_date:
            continue
        candidates.append(v)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda v: (_to_date(getattr(v, "effective_date", None)) or date.min, _created_at_of(v)),
    )
```
This is a strict simplification: one code path (the existing `as_of`-branch logic) now handles both "current" and "historical" queries, exactly as Backend §30 already documents ("'the version effective at time T' is 'current version' parameterized by a point in time, never a separate historical path") — Phase 12 just finishes applying that principle to the *default* case too. No caller signature changes; `resolve_current_version(versions)` and `resolve_current_version(versions, as_of=None)` both now mean "as of today."

**Business rules made deterministic by this fix (write these down verbatim in the function's docstring and in the unit tests, Task 3):**
1. A version is a **candidate** for "current as of date D" iff `status == READY` AND (`effective_date IS NULL OR effective_date <= D`) AND (`expiration_date IS NULL OR expiration_date > D`).
2. Among candidates, the one with the **latest `effective_date`** wins (`NULL effective_date` ranks lowest — an undated version never beats a dated one).
3. Ties (identical `effective_date`, including two `NULL`s) break by **latest `created_at`**.
4. **No candidate** → returns `None` (not an error) — the document is excluded from that query's result set for that date, exactly as Backend §30 already documents for the temporal case.
5. **Multiple READY versions with overlapping or missing effective dates can exist simultaneously** — this function still deterministically picks exactly one per the rules above; it does **not** flag the overlap as a problem. (Overlap/ambiguity surfacing is Phase 13's Conflict Detection concern, not Phase 12's — do not add conflict-flagging logic here.)
6. `status != READY` (any other status, including `FAILED`) is **never** a candidate, regardless of `effective_date`.

### 8.5 Effective Dates — Validation (new)

Add `validate_effective_window(effective_date: date | None, expiration_date: date | None) -> None` to `app/domain/versioning.py`:
```python
def validate_effective_window(effective_date: date | None, expiration_date: date | None) -> None:
    if effective_date is not None and expiration_date is not None and expiration_date < effective_date:
        raise ValidationError(
            "expiration_date must be on or after effective_date.",
            field="expiration_date",
        )
```
Wire into `DocumentService` upload/new-version path (`document_service.py`, immediately before the `if effective_date:` block at line ~314) — parse `expiration_date` the same way `effective_date` is currently parsed (it is **not currently accepted as a form field at all**; confirm during implementation whether the upload endpoint needs a new optional `expiration_date: Form(None)` parameter added alongside the existing `effective_date` one — check `app/api/documents.py`'s `upload_document` signature; if `expiration_date` is not currently an accepted form field, add it, since Phase 12's versioning semantics require it to be settable, not just readable). Malformed dates now raise `ValidationError` (422) instead of being silently ignored — this is a behavior change from "silently ignore bad dates" to "reject bad dates," which is the correct behavior per the roadmap and is safe because no legitimate caller relies on silent acceptance of malformed input.

### 8.6 Historical Queries

No new mechanism needed — `resolve_current_version(versions, as_of=<date>)` (already used by `AuthorizationService.resolve_allowed_documents(scope=VersionScope(as_of=...))`) is the single implementation for both "current" and "as of a specific date" queries, per §8.4's fix. Phase 12 additionally needs:

**New domain function** `classify_version_state(version, all_versions: list, today: date | None = None) -> Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]` in `app/domain/versioning.py` (Backend §47's documented, deliberately-not-stored classification):
```python
def classify_version_state(version, all_versions: list, today: date | None = None) -> str:
    if version.status != "READY":
        return "SCHEDULED" if (version.effective_date and version.effective_date > (today or date.today())) else "SUPERSEDED"
    current = resolve_current_version([v for v in all_versions if v.status == "READY"], as_of=today)
    if current is not None and current.id == version.id:
        return "CURRENT"
    eff = version.effective_date
    if eff is not None and eff > (today or date.today()):
        return "SCHEDULED"
    return "SUPERSEDED"
```
Used by: `GET /documents/{id}/versions` (enrich `DocumentVersionDetail` with a new `state` field — see §11 Task 2) and the frontend version selector (§15).

---

## 9. Comparison Architecture

### 9.1 Comparison Lifecycle

```
PENDING  →  PROCESSING  →  COMPLETED
                │
                └────────→  FAILED
```
Four states, matching `document_comparisons.status` (Database Architecture doc §25 exactly). This is **not** the same enum as `JobStatus` (which also has `RETRYING`) — a `processing_jobs` row backing a comparison can be `RETRYING` at the job level while `document_comparisons.status` stays `PROCESSING` at the user-visible level (retries are an implementation detail the comparison-level status does not need to expose). `ComparisonService` only ever writes `PENDING`/`PROCESSING`/`COMPLETED`/`FAILED` to `document_comparisons.status`.

### 9.2 Authorization

**New method**, `AuthorizationService.authorize_document_version(user, document_version_id, db) -> tuple[DocumentVersion, Document]`:
- Loads the `DocumentVersion` by ID; `NotFoundError` if missing.
- Loads its parent `Document`; `NotFoundError` if missing or `deleted_at IS NOT NULL` or `organization_id != user.organization_id` (cross-tenant access must look identical to "not found" — never leak existence via a 403, matching the existing convention seen in `test_document_processing.py`'s `test_status_endpoint_isolated_per_org`).
- Applies the **same** `access_level` rule already inline in `resolve_allowed_documents` (lines 158-176 of `authorization_service.py`) — extract it into a shared private function `_check_document_access_level(document, user) -> bool` used by both call sites, so there is exactly one implementation of the access-level rule in the codebase. `organization` → any org member; `private` → owner only, else `ForbiddenError`; `restricted` → treated as org-accessible for now (matching the existing Phase 7/16 placeholder behavior — do not harden this in Phase 12, that is explicitly Phase 16's job per the existing code comment).
- Returns `(version, document)` on success.

**`ComparisonService.get_or_create_comparison()` calls this exactly twice — once per side — before doing anything else, including before the reuse check.** If either call raises, the whole request fails with that exception (404 or 403) and **no** `document_comparisons` row is created or looked up. This directly satisfies the task's non-negotiable rule: never authorize only one side, and never leak whether a comparison exists for a pair the user isn't allowed to see (checking authorization before the reuse-lookup prevents an unauthorized user from learning "yes, these two versions have already been compared" via response-shape differences).

### 9.3 Comparison Reuse

Order-normalization (resolves Gap 4, §4.5):
```python
def _normalize_pair(version_a: DocumentVersion, version_b: DocumentVersion) -> tuple[DocumentVersion, DocumentVersion]:
    key_a = (version_a.document_id, version_a.version_number)
    key_b = (version_b.document_id, version_b.version_number)
    return (version_a, version_b) if key_a <= key_b else (version_b, version_a)
```
After normalization, `SELECT * FROM document_comparisons WHERE organization_id = :org AND document_a_version_id = :a AND document_b_version_id = :b` (the unique constraint backs this as an index). 
- **Found, any status:** return it as-is. The API layer (§13) decides the response code from `.status` — `200` if `COMPLETED`, `202` if `PENDING`/`PROCESSING` (do **not** enqueue a second job — a comparison already in flight must not be recomputed concurrently), and for `FAILED` see §20 Edge Cases ("re-request after failure").
- **Not found:** both versions must be `status == READY` (else `422 ValidationError`, per Roadmap "Error Handling: either version not READY → 422 at request time" and per the task's non-negotiable "no READY version" edge case) — then create.
- **Identical version on both sides** (`document_a_version_id == document_b_version_id` after normalization, i.e. before normalization the caller passed the same ID twice): reject with `422 ValidationError` before any DB write — comparing a version with itself is meaningless (§20 Edge Cases).

### 9.4 Section Alignment

**Input:** all `document_sections` rows for version A, all for version B (ordered by `sort_order`).
**Output:** a list of `(section_a | None, section_b | None, match_confidence)` tuples.
**Matching rules, in order (first match wins, per section):**
1. **Exact `section_number` match** (e.g., `"4.2"` == `"4.2"`) — confidence `1.0`. Strongest signal; sections rarely change numbering between versions of the same policy.
2. **Normalized-title match** among remaining unmatched sections: lowercase, strip punctuation/whitespace, exact string equality — confidence `0.9`. Catches renumbered-but-not-renamed sections.
3. **Title-embedding similarity fallback** among remaining unmatched sections: compute embeddings for all still-unmatched titles on both sides via the existing `EmbeddingProvider.embed()`, cosine similarity, greedy best-match-first pairing above threshold **0.86** (plan-authored default, not from any document — tune later if needed) — confidence = the similarity score. Catches renumbered *and* retitled sections.
4. **Unmatched after all three passes:** a section present only in B → candidate section-level `ADDED`; present only in A → candidate section-level `REMOVED`.

**Unmatched-section handling:** each unmatched section becomes exactly one `comparison_changes` row at the section granularity (no further text/semantic comparison needed — there is nothing to diff against). `old_chunk_id`/`old_text` NULL for `ADDED`; `new_chunk_id`/`new_text` NULL for `REMOVED` (matches the DB schema's nullable-FK design exactly).

**Large sections:** no special handling at the alignment stage (alignment only compares titles/numbers, which are always short) — large-section handling applies at the semantic-comparison stage (§9.6).

**Renumbered/renamed/moved sections:** covered by passes 2–3 above. "Moved" (same title/number, different `sort_order`/position in the tree) is **not** treated as a change by itself — position is not diffed; only content is. If a document's section tree is reordered but content is untouched, sections still align correctly via number/title match and simply produce zero `comparison_changes` rows for those sections (no false-positive "moved" change type — `ADDED`/`REMOVED`/`MODIFIED` are the only three types persisted, per DB §25's CHECK constraint; there is no `MOVED` type and this plan does not add one).

**Failure behavior — pathological restructuring:** if fewer than **30%** of version B's sections achieve a match (any confidence) against version A (plan-authored threshold), abandon section-level alignment entirely: treat each whole version's full text (all pages concatenated) as a single pseudo-section, run text comparison + semantic comparison at that single level, and set `document_comparisons.summary["alignment_degraded"] = true` so the UI can disclose this ("Comparison covers whole-document text only — section structure could not be reliably matched between these versions.", matching the Frontend §6.10 pattern for disclosed truncation).

### 9.5 Text Comparison (deterministic)

For each matched section pair from §9.4, compare content at the **chunk level first, span level second**:
1. **Content-hash short-circuit:** if the section's chunks (in order) have pairwise-identical `content_hash` between A and B, the whole section is `UNCHANGED` — **stop here, no further processing, no `comparison_changes` row created.** This is the cheap pre-diff `document_chunks.content_hash` was added for.
2. **Normalization** (only when hashes differ, before the actual diff): collapse runs of whitespace to a single space, strip leading/trailing whitespace per line, normalize line endings to `\n`. This absorbs pure reformatting (e.g., a re-wrapped paragraph with identical words) without a false `MODIFIED`.
3. **Sentence-level diff** on normalized text (e.g., split on sentence boundaries, then a sequence-alignment/LCS-style comparison — implement with Python's standard library `difflib.SequenceMatcher` at the sentence-token level; this is sufficient and avoids adding a new dependency) to identify which spans actually differ.
4. If normalized text is identical after step 2 → `UNCHANGED`, not persisted (formatting-only changes are absorbed here, matching the task's requirement that "formatting-only change → MINOR" is instead resolved as **not a change at all** when it is *purely* whitespace/line-wrap — a genuine formatting change that alters visible text, e.g. adding bullet markers that change words, still reaches step 3 and can be classified `MINOR` at the severity stage).
5. If any span differs → `MODIFIED` candidate, proceeds to §9.6.

### 9.6 Semantic Comparison

**When triggered:** exactly once per `MODIFIED` candidate section from §9.5 (not per sentence — bundle the whole section's old/new text into one call to bound total LLM calls per comparison).

**Input to the LLM** (via `LLMProvider.generate()`, reusing the existing single-method interface — no new provider capability needed): a constrained system prompt (new file or addition to `app/rag/prompts.py`) instructing the model to classify whether the meaning changed, given `old_text` and `new_text` for one section. Example prompt intent: *"Given OLD and NEW versions of one document section, determine whether the underlying meaning/obligation changed (MATERIAL) or whether this is a stylistic/non-substantive rewording (STYLISTIC). Respond with ONLY a JSON object: `{"materiality": "material"|"stylistic", "rationale": "<one sentence>"}`."*

**Output validation:** reuse the exact defensive-parsing pattern already used by `query_analyzer.py::parse_analyzer_output()` (strip markdown fences, locate the JSON object, validate the `materiality` field is one of the two allowed literal values) — do not trust free-form LLM output structurally; on a parse failure, treat `materiality` as `None` (see failure handling below), never crash the pipeline.

**Bounding large sections:** truncate `old_text`/`new_text` sent to the LLM to a token budget (plan-authored: **2,000 tokens** each side, reuse the existing `app/ingestion/tokenizer.py` token-counting utility) — truncation is for the *semantic* call only, never for the deterministic text diff (§9.5), and the truncation is disclosed: set a `truncated: true` flag alongside that section's classification internally (surfaced to the UI as part of the section's change entry if truncation occurred, so a user is never shown a materiality verdict computed on partial text without being told).

**Failure handling:** if the LLM call raises (`LLMProviderError`, timeout, malformed output after the retry pattern above) — **the pipeline continues.** `materiality` is recorded as `None` for that section's severity computation (§9.8's rule set has an explicit `None` branch — never "no severity" or job failure just because one semantic call failed). This matches the roadmap's explicit failure-mode guidance: "LLM outage → RETRYING/resume from last classified pair" at the *job* level (transient infra failure retries the whole job per the existing `RetryableJobError` policy) but a *single section's* semantic-call failure degrades gracefully within a still-succeeding job, consistent with "comparison can continue without semantic analysis" per this plan's design (§9.8).

**Deterministic comparison remains the source of truth:** the semantic call can only ever influence **severity** (§9.8) — it never changes `change_type` (`ADDED`/`REMOVED`/`MODIFIED` are decided purely by §9.4/§9.5) and it can never invent a change that the deterministic diff did not already detect. This satisfies the task's explicit requirement: "Do not allow an LLM to arbitrarily invent changes."

### 9.7 Change Detection

Per §9.4/§9.5: a `comparison_changes` row is created for every section-level `ADDED`/`REMOVED` and every span-level `MODIFIED`. **`UNCHANGED` is never persisted** — this is a hard rule from the Database Architecture doc ("only differences are stored, keeping the table's volume proportional to actual changes") and is enforced in code by simply never constructing a row for that case, not by a soft filter.

### 9.8 Change Classification

**`change_type` (deterministic, decided at §9.4/§9.5, never revisited afterward):**
- `ADDED` — section/span exists only in version B.
- `REMOVED` — section/span exists only in version A.
- `MODIFIED` — matched section/span with detected textual difference.

**Severity (`domain/comparison_rules.py`, pure function) — plan-authored concrete rule set** (the source documents specify the three qualitative inputs and their directional influence, but never exact thresholds; this is this plan's first implementable version, explicitly not a verbatim requirement):

```python
def classify_severity(
    *,
    change_type: Literal["ADDED", "REMOVED", "MODIFIED"],
    semantic_materiality: Literal["material", "stylistic"] | None,
    is_critical_section: bool,
    proportion_changed: float,   # 0.0–1.0, word-level changed-token ratio
) -> Literal["MAJOR", "MODERATE", "MINOR"]:
    if is_critical_section:
        return "MAJOR"
    if change_type in ("ADDED", "REMOVED"):
        return "MODERATE"
    if semantic_materiality == "material":
        return "MAJOR" if proportion_changed >= 0.3 else "MODERATE"
    if semantic_materiality == "stylistic":
        return "MINOR"
    # semantic stage unavailable/failed — proportion alone, capped below MAJOR
    return "MODERATE" if proportion_changed >= 0.5 else "MINOR"
```

**`is_critical_section`:** looks up `organization.settings["comparison"]["critical_sections"]` (a new, plan-defined convention inside the existing `organizations.settings` JSONB — no migration needed) — a list of case-insensitive substrings matched against the section's `title` or `section_number` (default: empty list, meaning no section is automatically critical until an org configures one). Document this convention in the `ComparisonService` docstring since it is a new, previously-undocumented key inside an existing JSONB column.

**`proportion_changed`:** `(count of changed word-tokens) / (max(word count of old_text, word count of new_text))`, computed from the same `difflib.SequenceMatcher` result already produced in §9.5 (no extra computation pass needed) — for `ADDED`/`REMOVED` this is trivially `1.0` but is not used in that branch (handled directly by the `change_type` check above).

**Worked examples (for the unit tests in §16.1 and to sanity-check the rule set against the task's own examples):**
| Scenario | change_type | materiality | critical | proportion | → severity |
|---|---|---|---|---|---|
| "5 business days" → "7 business days" in a non-critical section | MODIFIED | material | false | 0.15 | **MODERATE** |
| Same, but section is in `critical_sections` (e.g. "Approval Timeline") | MODIFIED | material | true | 0.15 | **MAJOR** |
| Reworded sentence, same meaning ("must" → "shall") | MODIFIED | stylistic | false | 0.4 | **MINOR** |
| Whole new clause added to a section | MODIFIED | material | false | 0.6 | **MAJOR** |
| A new section added (e.g. new appendix) | ADDED | n/a | false | n/a | **MODERATE** |
| A new *critical* section added | ADDED | n/a | true | n/a | **MAJOR** |
| Typo fix, single-character diff | MODIFIED | stylistic | false | 0.02 | **MINOR** |
| Semantic call failed (provider outage), text differs moderately | MODIFIED | `None` | false | 0.55 | **MODERATE** |

### 9.9 Citation Mapping

**Two distinct provenance surfaces, deliberately not unified (this is not a gap — the docs already imply this split, this plan makes it explicit):**

1. **`/compare` UI provenance** (`ChangeCard` → "View Sources" showing *both* old and new text simultaneously): backed directly by `comparison_changes.old_chunk_id`/`new_chunk_id` + denormalized `old_text`/`new_text` snapshots — **not** the `citations` table. A single `citations` row can only reference one chunk, so it cannot represent "here is the old source AND the new source" in one record; `comparison_changes` already carries both nullable FKs plus text snapshots for exactly this reason (Database doc §25: `SET NULL` on chunk delete specifically so a comparison "should outlive the chunks it was computed from"). Resolving `old_chunk_id`/`new_chunk_id` into page/section/document/version display data is a simple join at read time (`GET /compare/{id}/changes` response, §13) — reuse the exact same join shape `citations.py`/`context_builder.py` already use for chunk→page→section→version→document resolution, just applied to two chunk IDs instead of one.
2. **Chat `CHANGE_DETECTION` narration provenance** (a normal assistant `Message` in a conversation): reuses the **existing** `citations` table completely unmodified. Each sentence of the narrated summary that references a specific old or new source gets a normal `Citation` row pointing at that one chunk (old *or* new, whichever the sentence is about) — exactly like any other RAG answer. No schema change needed for this path; it is a pure consumer of the existing citation-resolution pipeline (§14).

Every `comparison_changes` row always carries enough denormalized data (`section`, `old_text`/`new_text`, and via the chunk FK join, page number and document/version identity) to render "Old: [document] v[N] p.[X] — '[quoted text]'" / "New: [document] v[M] p.[Y] — '[quoted text]'" without any additional query beyond the one join — satisfying the task's explainability requirement directly from the persisted row.

---

## 10. Database Changes

**One migration:** `backend/alembic/versions/012_document_comparisons.py` (down_revision = `011`).

### Task 1 — Migration: `document_comparisons`, `comparison_changes`, `processing_jobs` extension

```sql
-- document_comparisons
CREATE TABLE document_comparisons (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    document_a_version_id    UUID NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    document_b_version_id    UUID NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    status                   TEXT NOT NULL DEFAULT 'PENDING'
                             CHECK (status IN ('PENDING','PROCESSING','COMPLETED','FAILED')),
    summary                  JSONB,
    error_message            TEXT,
    requested_by             UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at             TIMESTAMPTZ,
    CONSTRAINT uq_document_comparisons_pair UNIQUE (organization_id, document_a_version_id, document_b_version_id),
    CONSTRAINT ck_document_comparisons_distinct_versions CHECK (document_a_version_id <> document_b_version_id)
);
CREATE INDEX ix_document_comparisons_org_status ON document_comparisons (organization_id, status);
CREATE INDEX ix_document_comparisons_a ON document_comparisons (document_a_version_id);
CREATE INDEX ix_document_comparisons_b ON document_comparisons (document_b_version_id);

-- comparison_changes
CREATE TABLE comparison_changes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    comparison_id    UUID NOT NULL REFERENCES document_comparisons(id) ON DELETE CASCADE,
    change_type      TEXT NOT NULL CHECK (change_type IN ('ADDED','REMOVED','MODIFIED')),
    severity         TEXT NOT NULL CHECK (severity IN ('MAJOR','MODERATE','MINOR')),
    section          TEXT,
    old_chunk_id     UUID REFERENCES document_chunks(id) ON DELETE SET NULL,
    new_chunk_id     UUID REFERENCES document_chunks(id) ON DELETE SET NULL,
    old_text         TEXT,
    new_text         TEXT,
    truncated        BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_comparison_changes_added_no_old CHECK (change_type <> 'ADDED' OR old_chunk_id IS NULL),
    CONSTRAINT ck_comparison_changes_removed_no_new CHECK (change_type <> 'REMOVED' OR new_chunk_id IS NULL)
);
CREATE INDEX ix_comparison_changes_comparison_severity ON comparison_changes (comparison_id, severity);
CREATE INDEX ix_comparison_changes_comparison_section ON comparison_changes (comparison_id, section);

-- processing_jobs extension (Gap 3, §4.5)
ALTER TABLE processing_jobs ADD COLUMN comparison_id UUID REFERENCES document_comparisons(id) ON DELETE CASCADE;
ALTER TABLE processing_jobs ADD CONSTRAINT ck_processing_jobs_comparison_pairing
    CHECK ((job_type = 'COMPARISON') = (comparison_id IS NOT NULL));
CREATE INDEX ix_processing_jobs_comparison_id ON processing_jobs (comparison_id);
```

**Notes:**
- `document_a_version_id`/`document_b_version_id` use `ON DELETE RESTRICT` (not `CASCADE`) — a version that has been compared cannot be silently deleted out from under the comparison record, consistent with the citations table's RESTRICT policy for the same reason (the comparison "should outlive" transient chunk deletions per `old_chunk_id`/`new_chunk_id` `SET NULL`, but the version-level FK on `document_comparisons` itself is RESTRICT because there is currently no documented version-deletion flow at all in this codebase — restricting is the safe default until one exists; see §20 "Deleted version" edge case).
- `truncated` column (not in the Database Architecture doc's original spec, added by this plan) supports §9.6's disclosed-truncation requirement — a small, justified schema addition beyond the doc's literal column list.
- No soft-delete column on either new table — the Database Architecture doc's §29 soft-delete enumeration does not name the comparison domain at all (Gap noted in research, not resolved by any source doc). **This plan's resolution:** treat `document_comparisons`/`comparison_changes` as hard-delete-only (no `deleted_at`), consistent with `messages`/`citations`/`audit_logs` (immutable historical record convention) rather than `documents`/`conversations` (soft-deletable). If a document is ever hard-deleted in a future phase, the `RESTRICT` FK means that deletion flow must explicitly handle (or block on) existing comparisons — out of scope to design further here; flag it in that future phase's plan.
- **Update `backend/alembic/env.py`**: add `DocumentComparison`, `ComparisonChange` to the explicit model-import list (required for autogenerate parity checks to see them; this migration is hand-written, so the import is for future autogenerate runs, not this migration itself).

**Models** (`backend/app/models/comparison.py`, new file):
```python
class DocumentComparison(Base):
    __tablename__ = "document_comparisons"
    # columns per SQL above; relationships: organization, document_a_version, document_b_version,
    # requested_by_user, changes (back_populates, cascade="all, delete-orphan", order_by=[severity, created_at])

class ComparisonChange(Base):
    __tablename__ = "comparison_changes"
    # columns per SQL above; relationships: comparison (back_populates="changes"), old_chunk, new_chunk (both lazy="noload")
```
Follow the exact style of `app/models/document.py` (typed `Mapped[...]`, `mapped_column`, `TYPE_CHECKING` imports for cross-refs, `__repr__`).

**`processing_jobs` model update** (`app/models/processing_job.py`): add
```python
comparison_id: Mapped[Optional[str]] = mapped_column(
    UUID(as_uuid=False),
    ForeignKey("document_comparisons.id", ondelete="CASCADE"),
    nullable=True,
    comment="Set only for job_type=COMPARISON; document_version_id holds the anchor (A) version",
)
```
plus the matching `Index("ix_processing_jobs_comparison_id", "comparison_id")` in `__table_args__`.

**Acceptance criteria:** `alembic upgrade head` succeeds against a clean DB (extends `backend/tests/integration/test_migrations.py`); `alembic downgrade -1` cleanly reverses it; both new tables' CHECK constraints reject invalid `change_type`/`severity`/`status` values (add to `test_migrations.py`); `ProcessingJob.comparison_id` CHECK correctly rejects a `COMPARISON` job with `comparison_id IS NULL` and rejects a non-`COMPARISON` job with `comparison_id IS NOT NULL`.

---

## 11. Backend Changes

### Task 2 — Domain layer: versioning fixes + comparison rules

**Files:**
- `backend/app/domain/versioning.py` — **MODIFY**: fix `resolve_current_version()` per §8.4; add `validate_effective_window()` per §8.5; add `classify_version_state()` per §8.6.
- `backend/app/domain/comparison_rules.py` — **CREATE**: `classify_severity()` per §9.8, plus a small `compute_proportion_changed(old_text: str, new_text: str) -> float` helper using `difflib.SequenceMatcher` (word-tokenized).
- `backend/app/domain/section_alignment.py` — **CREATE**: pure alignment functions per §9.4 (`align_sections(sections_a, sections_b, embedding_provider) -> list[SectionAlignment]`, where `SectionAlignment` is a small dataclass `(section_a, section_b, confidence, match_method)`). Kept pure/testable by accepting an already-constructed embedding lookup (a `dict[str, Vector]` precomputed by the caller) rather than calling the provider itself inside this module — matches the existing convention that `domain/*.py` has no I/O.
- `backend/app/domain/text_diff.py` — **CREATE**: `diff_text(old: str, new: str) -> TextDiffResult` (normalization + `difflib`-based span diff per §9.5), returning whether the result is `UNCHANGED` or `MODIFIED` plus the changed-token count needed for `proportion_changed`.

**Dependencies:** none (pure functions, no I/O, per existing `domain/` convention).

**Acceptance criteria:** every function has 100% branch coverage in `backend/tests/unit/` (see §16.1); no function imports SQLAlchemy, `httpx`, or any provider class.

### Task 3 — Fix `resolve_current_version`, extend its tests

**Files:**
- `backend/app/domain/versioning.py` — apply the §8.4 fix.
- `backend/tests/unit/test_versioning.py` — **MODIFY**: remove/replace any existing test asserting the old (broken) `is_current`-flag-first behavior; add cases: no-`as_of` call with a future-`effective_date` READY version present alongside a currently-effective one (must pick the currently-effective one, not the future one); no-`as_of` call with only a future-dated version (must return `None`, i.e. "no current version yet" — not the scheduled one); overlapping effective windows (deterministic tie-break by `created_at` verified); `status != READY` versions never selected regardless of dates.

**Dependencies:** Task 2.

**Acceptance criteria:** all new/modified unit tests pass; **no other file needs to change** — `authorization_service.py` and `workers/jobs.py` automatically get correct behavior because they call the same function.

### Task 4 — Authorization: single document/version authorizer

**Files:**
- `backend/app/services/authorization_service.py` — **MODIFY**: extract `_check_document_access_level(document, user) -> bool` from the inline logic at lines 158-176; add `authorize_document_version(user, document_version_id, db) -> tuple[DocumentVersion, Document]` per §9.2.
- `backend/tests/unit/test_authorization_service.py` — **CREATE** (no such file exists today; the existing authorization tests live in `test_permissions.py` at the RBAC-only level) — unit-test the extracted access-level function and `authorize_document_version` against organization/restricted/private × owner/non-owner × correct-org/wrong-org matrices, using lightweight stub `User`/`Document`/`DocumentVersion` objects (no DB needed — mock the repository call or, if that's awkward given the current async-session-coupled implementation, write this as an integration test instead under `backend/tests/integration/` using the real DB — **decide based on how `resolve_allowed_documents` is already tested**; if no existing test isolates DB calls with stubs, follow that file's precedent and make this an integration test).

**Dependencies:** none beyond existing `authorization_service.py`.

**Acceptance criteria:** authorized-both-sides passes; authorized-A-only, authorized-B-only, and authorized-neither all raise (never silently succeed); cross-org version ID raises `NotFoundError` (not `ForbiddenError` — no existence leakage).

### Task 5 — `ComparisonRepository`

**File:** `backend/app/repositories/document_comparison_repository.py` — **CREATE**, following the existing one-repository-per-aggregate pattern (`document_chunk_repository.py`, `document_section_repository.py`).

Methods:
- `get_by_pair(organization_id, version_a_id, version_b_id) -> DocumentComparison | None` (the reuse-check query, §9.3).
- `create(organization_id, version_a_id, version_b_id, requested_by) -> DocumentComparison` (status=PENDING).
- `get_by_id(comparison_id) -> DocumentComparison | None`.
- `update_status(comparison, status, *, summary=None, error_message=None, completed_at=None)`.
- `add_change(comparison_id, **fields) -> ComparisonChange` (used incrementally by the worker, §9.4-9.9).
- `list_changes(comparison_id, *, severity=None, section=None) -> list[ComparisonChange]`.
- `get_change(comparison_id, change_id) -> ComparisonChange | None`.

**Dependencies:** Task 1 (models must exist).

**Acceptance criteria:** unit/integration tests (whichever this codebase's convention favors for repositories — check `backend/tests/integration/test_repositories.py` as precedent; it tests `Organization`/`User` repos directly against the real test DB, so follow that: put comparison-repository tests in `test_repositories.py` or a new `test_comparison_repository.py` alongside it) cover reuse-lookup hit/miss, incremental `add_change` calls, and severity/section filtering on `list_changes`.

### Task 6 — `ComparisonService`

**File:** `backend/app/services/comparison_service.py` — **CREATE**, static-method class following `DocumentService`/`JobService` convention.

```python
class ComparisonService:
    @staticmethod
    async def get_or_create_comparison(
        *, user: User, document_a_version_id: str, document_b_version_id: str, db: AsyncSession,
    ) -> tuple[DocumentComparison, bool]:  # (comparison, created)
        # 1. authorize both sides (§9.2) — raises on failure, nothing persisted
        # 2. reject identical IDs (422) — §9.3
        # 3. normalize pair order (§9.3 / Gap 4)
        # 4. reuse check via ComparisonRepository.get_by_pair — return (existing, False) if found
        # 5. both versions must be READY — else 422
        # 6. create PENDING document_comparisons row
        # 7. create processing_jobs row (job_type=COMPARISON, comparison_id=new_id,
        #    document_version_id=document_a_version_id) via JobService (reuse existing
        #    JobService.create_for_version-style helper, or add a JobService.create_for_comparison
        #    variant — see Task 7)
        # 8. commit; enqueue_after_commit (reuse JobService.enqueue_after_commit exactly)
        # 9. return (new_comparison, True)
```

**Dependencies:** Tasks 1, 3, 4, 5, 7.

**Acceptance criteria:** covered fully by §16.2 integration tests — reuse returns identical row without a second job; unauthorized-either-side raises before any write; not-READY version raises 422; identical-version raises 422; concurrent duplicate requests for the same pair do not create two `document_comparisons` rows (rely on the unique constraint — a second concurrent insert attempt raises `IntegrityError`, which the service catches and re-fetches via `get_by_pair` instead of propagating a 500 — implement this catch-and-refetch explicitly, since Arq/Postgres concurrency means two near-simultaneous `POST /compare` calls for the same pair are a realistic race, unlike the version-number race in §8.1 which this plan deliberately does not fix).

### Task 7 — `JobService` extension for comparison jobs

**File:** `backend/app/services/job_service.py` — **MODIFY**: add `create_for_comparison(db, *, organization_id, comparison_id, anchor_version_id, job_type=JobType.COMPARISON) -> ProcessingJob`, mirroring the existing `create_for_version` but setting `comparison_id` and using `anchor_version_id` for the required `document_version_id` column (per §10's Gap-3 resolution). `enqueue_after_commit` is reused completely unmodified (it only needs the job ID).

**Dependencies:** Task 1.

**Acceptance criteria:** unit test confirms the created row has both `document_version_id` (= anchor/A) and `comparison_id` populated, satisfying the new CHECK constraint.

### Task 9 — Semantic comparison + narration prompts

**File:** `backend/app/rag/prompts.py` — **MODIFY**: add `SEMANTIC_COMPARISON_SYSTEM_PROMPT`, `SEMANTIC_COMPARISON_USER_TEMPLATE` (per §9.6), `CHANGE_NARRATION_SYSTEM_PROMPT`, `CHANGE_NARRATION_USER_TEMPLATE` (per §14) — added alongside the existing `ANALYZER_SYSTEM_PROMPT` in the same file, following its exact style (constrained JSON-out instructions, explicit "respond with ONLY the JSON object" framing).

**Dependencies:** none.

**Acceptance criteria:** prompt-parsing unit tests (§16.1) exercise both the happy path and malformed-output path for each new prompt's parser function (colocated in `domain/comparison_rules.py` or a new `app/rag/comparison_parsing.py`, following `query_analyzer.py::parse_analyzer_output`'s pattern of keeping the parser pure/testable separately from the network call).

---

## 12. Background Worker Changes

### Task 8 — Comparison worker handler + dispatch branch

**Files:**
- `backend/app/workers/jobs.py` — **MODIFY**:
  1. In `run_processing_job()`, immediately after the "Claim" section (after line ~464) and before the existing "Load referenced entity FRESH + validate tenancy" block (lines ~466-519), add:
     ```python
     if JobType(job.job_type) is JobType.COMPARISON:
         return await _run_comparison_job(ctx, job, session)
     ```
     `_run_comparison_job` is a new private function performing: load `DocumentComparison` by `job.comparison_id` (404-equivalent → `DeterministicJobError` if missing), verify `comparison.organization_id == job.organization_id` (tenancy check, same pattern as the existing document/version check), transition `document_comparisons.status` to `PROCESSING`, call `handle_comparison(ctx, job, comparison, session)`, then on success set `COMPLETED` + `completed_at` + `summary`; on `DeterministicJobError` set `FAILED` + `error_message`; on other exceptions, apply the existing `_handle_failure` retry logic (reused as-is — it is already generic over `job`/`ver_repo`/`version.id`; a small refactor may be needed to make it generic over "the entity being processed" if it currently hard-codes version-specific fields — check `_handle_failure`'s signature during implementation and adapt minimally, e.g. make the version-status-failure side-effect conditional on `job.job_type` not being `COMPARISON`, since a comparison job failure does not transition any `DocumentVersion`'s status).
  2. Add `handle_comparison(ctx, job, comparison, session)` implementing §9.4–§9.9 in sequence: load both versions' sections/chunks, run alignment, run text comparison, run semantic comparison (bounded, per §9.6, using `get_llm_provider()`/`get_embedding_provider()` exactly like existing handlers do), run classification/severity, persist `comparison_changes` **incrementally per matched section** (one `INSERT` + `session.commit()` per section processed, not one giant end-of-job batch — per the existing idempotency convention documented at Backend §49 and explicitly required by the roadmap: "Persist per-section incrementally (idempotent, resumable)"), update `job.progress`/`progress_message` after each section (reusing `ProcessingJobRepository.mark_progress`, already used by `handle_indexing`).
  3. Add `JobType.COMPARISON: handle_comparison` to the `HANDLERS` dict (line ~410-415) — **but note** `handle_comparison`'s signature `(ctx, job, comparison, session)` differs from the existing `JobHandler` type alias `(ctx, job, version, document, session)`. Either (a) widen the `JobHandler` type alias to a `Union`/`Protocol` covering both signatures, or (b) do not register `handle_comparison` in the shared `HANDLERS` dict at all and instead call it directly from the `_run_comparison_job` branch added in step 1 (since that branch already special-cases dispatch before the generic `HANDLERS.get(...)` lookup at line ~522 is ever reached). **Recommendation: (b)** — simpler, avoids polluting the existing handler-type contract that every other job type still relies on, and the early `if job.job_type is JobType.COMPARISON: return await _run_comparison_job(...)` branch already makes the generic dispatch path unreachable for comparison jobs, so `HANDLERS` never needs a `COMPARISON` entry at all.
  4. **Resumability:** because `handle_comparison` persists per-section and re-reads `comparison_changes` already written for this `comparison_id` at the start of a retry (skip sections already present, matching the existing `handle_extraction`'s "resume-after-crash" pattern verified by `test_extraction_pipeline.py`), a retried/resumed comparison job never duplicates `comparison_changes` rows.

**Dependencies:** Tasks 1, 2, 5, 6, 9.

**Acceptance criteria:** §16.2/§16.5 integration + worker tests — full pipeline run against a seeded two-version fixture produces the expected `comparison_changes` rows; killing the worker mid-run (simulated by raising after N sections persisted) and re-running resumes without duplicating already-persisted changes; a semantic-comparison LLM failure (via `StubLLMProvider` configured to raise) does not fail the job, only omits `materiality` for that section (§9.6); retry-exhaustion after repeated transient failures marks the job (and `document_comparisons.status`) `FAILED` with a populated `error_message`.

---

## 13. API Changes

**File:** `backend/app/api/compare.py` — **CREATE** new router, `prefix="/compare"`, registered in `app/main.py` alongside the existing routers (check `main.py` for the exact `app.include_router(...)` pattern used for `documents.router` etc. and mirror it).

| Method | Route | Auth | Permission | Request | Response | Status |
|---|---|---|---|---|---|---|
| `POST` | `/compare` | required | `comparison:create` (existing `PermissionKey.COMPARISON_CREATE`) | `ComparisonCreateRequest {document_a_version_id: str, document_b_version_id: str}` | `ComparisonResponse` | `202` if newly created; `200` if an existing `COMPLETED` comparison is returned; `200` if an existing `PENDING`/`PROCESSING` comparison is returned (client polls `GET /compare/{id}` — same pattern as `GET /documents/{id}/status` polling); `422` if either version not `READY` or IDs identical; `403`/`404` per §9.2 |
| `GET` | `/compare/{comparison_id}` | required | `document:read` (checked against **both** sides again at read time — see note below) | — | `ComparisonResponse` | `200`; `404` if not found or either side not authorized |
| `GET` | `/compare/{comparison_id}/changes` | required | `document:read` | Query: `severity: Optional[str]`, `section: Optional[str]` | `ComparisonChangesResponse {items: list[ComparisonChangeItem]}` | `200`; `404` per above |
| `GET` | `/compare/{comparison_id}/changes/{change_id}` | required | `document:read` | — | `ComparisonChangeItem` | `200`; `404` |

**Re-checking authorization on every read (not just at creation time):** a `document_comparisons` row can outlive a later `access_level` change on either source document (e.g., a document becomes `private` after the comparison was created by a different user who was, at the time, authorized). All three `GET` endpoints re-run `AuthorizationService.authorize_document_version()` for **both** `document_a_version_id` and `document_b_version_id` before returning anything — treat a failure on *either* side as `404` (not `403`, matching the existing project-wide convention of never leaking resource existence via a differentiated status code). This directly satisfies the task's "no information leakage through comparison APIs" requirement and its authorization-matrix requirement (`A only` / `B only` / `neither` / `both` → allowed only for `both`).

**Idempotency:** `POST /compare` is naturally idempotent via the reuse check (§9.3) — calling it N times for the same pair returns the same `comparison_id` and never enqueues more than one job. No `Idempotency-Key` header mechanism exists elsewhere in this codebase (not found during research) — do not introduce one just for this endpoint; the reuse-by-pair mechanism already provides the needed guarantee.

**Schemas** (`backend/app/schemas/comparison.py` — **CREATE**, `_OrmBase` convention):
```python
class ComparisonCreateRequest(BaseModel):
    document_a_version_id: str
    document_b_version_id: str

class ComparisonSummary(BaseModel):
    total: int = 0
    major: int = 0
    moderate: int = 0
    minor: int = 0
    alignment_degraded: bool = False

class ComparisonResponse(_OrmBase):
    id: str
    organization_id: str
    document_a_version_id: str
    document_b_version_id: str
    status: str
    summary: Optional[ComparisonSummary] = None
    error_message: Optional[str] = None
    requested_by: str
    created_at: datetime
    completed_at: Optional[datetime] = None

class ComparisonChangeItem(_OrmBase):
    id: str
    comparison_id: str
    change_type: str
    severity: str
    section: Optional[str] = None
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    truncated: bool = False
    # Resolved provenance (populated by the service layer, not a direct ORM passthrough):
    old_source: Optional[SourceRef] = None
    new_source: Optional[SourceRef] = None

class SourceRef(BaseModel):
    document_id: str
    document_version_id: str
    document_name: str
    version_number: int
    page_number: int
    section: Optional[str] = None

class ComparisonChangesResponse(BaseModel):
    items: list[ComparisonChangeItem]
```

**Pagination:** per Gap noted during research (Backend/DB docs mandate keyset pagination on list endpoints generally; neither Phase 12 doc section nor the Frontend mockups show pagination on the changes list). **Resolution:** `GET /compare/{id}/changes` returns the full list unpaginated for V1 — a single document's comparison realistically produces tens, not thousands, of changes (bounded by section count), so keyset pagination is unnecessary complexity here; do not add it. Document this explicitly as a deliberate simplification, not an oversight, if asked.

**Dependencies:** Task 6 (service), Task 1 (models/schemas).

**Acceptance criteria:** §16.3/§16.4 API tests cover every row of the table above plus the authorization matrix.

---

## 14. CHANGE_DETECTION Integration

**Problem:** `app/rag/query_analyzer.py` already classifies `COMPARISON`/`CHANGE_DETECTION` correctly; `app/services/ask_service.py:268` calls `analyze_query()` but never branches on `analysis.intent` — every intent proceeds through the same generic RAG path. No source document specifies **how two document versions get resolved from a natural-language chat message** — this is a genuine gap this plan must close with a deterministic (non-LLM) algorithm, per the task's explicit requirement ("Do not delegate version selection to the LLM").

**New deterministic resolution algorithm** (add to `ComparisonService` as `resolve_comparison_targets_from_chat(conversation, analysis, db) -> tuple[str, str] | None`):

1. If the conversation's scope (`conversation.scope_type`) is `selected_documents` **and** exactly **2** documents are currently in scope (`conversation_documents` rows with `removed_at IS NULL`) → resolve each document's version using `resolve_current_version(as_of=...)`, where `as_of` comes from `analysis.temporal_scope` **only if** it can be unambiguously assigned to one of the two documents (see step 2's tie-breaking note) — otherwise use each document's plain current version (no `as_of`). Return `(version_id_1, version_id_2)`.
2. Else if scope is `current_document` (exactly one document) **and** `analysis.temporal_scope` names **two distinct years/dates** in the raw query (this requires a small extension to the analyzer's temporal-scope extraction — the current `temporal_scope` shape (`{"year": int}` or `{"relative": str}`) only carries **one** value; extend `QueryAnalysis`/`_parse_temporal_scope` to optionally return a `temporal_scope_secondary` field when the analyzer detects two distinct temporal references in one query, e.g. "changed between 2025 and 2026" — this is a small, additive change to `query_analyzer.py`, not a redesign) → resolve that single document's version as-of each of the two dates via `resolve_current_version`. If both resolve to the **same** version (e.g., only one version ever existed, or both dates fall in the same version's window), this is not a valid comparison — fall through to step 3's failure behavior.
3. **Otherwise:** do not guess. Return `None`. The caller (`AskService`/`ChatService`) responds with a clarifying message (not a fabricated comparison) — e.g., a `SYSTEM`-role message (reusing the existing `SYSTEM`-role scope-change-marker pattern from Phase 11) or simply an `ASSISTANT` message stating "I can compare two versions, but I need you to specify which two — try selecting exactly two documents, or ask about two specific years/dates for one document." This is a deterministic, code-driven fallback — never an LLM-invented guess at which versions to compare.

**Routing wiring** (`app/services/ask_service.py`, near line 268, and `app/services/chat_service.py:402`):
```python
analysis = await analyze_query(question, provider=llm)
if analysis.intent in ("COMPARISON", "CHANGE_DETECTION"):
    targets = await ComparisonService.resolve_comparison_targets_from_chat(conversation, analysis, db)
    if targets is None:
        return <clarifying-response path>
    version_a_id, version_b_id = targets
    comparison, _ = await ComparisonService.get_or_create_comparison(
        user=user, document_a_version_id=version_a_id, document_b_version_id=version_b_id, db=db,
    )
    # if comparison.status != COMPLETED: either wait synchronously with a bounded timeout
    # (reuse the existing "trigger job, then poll" pattern is NOT appropriate inside a
    # synchronous chat turn) OR — RECOMMENDED — respond immediately with a SYSTEM/ASSISTANT
    # message: "Comparing these versions now — this can take a moment; ask again shortly"
    # and let the user re-ask (the comparison, once COMPLETED, is then served instantly via
    # the reuse check). Do not block the chat SSE stream on a multi-minute worker job.
    changes = await comparison_repo.list_changes(comparison.id)
    narration = await narrate_changes(changes, provider=llm)  # constrained LLM call, phrasing only
    # persist as a normal ASSISTANT Message with Citations (one per change referenced, old or new
    # chunk_id per §9.9) via the EXISTING message+citation persistence path (reuse, do not duplicate)
else:
    # existing standard RAG path, unchanged
```

**Narration LLM call** (`narrate_changes`, new function in `app/rag/generator.py` or a new `app/rag/comparison_narration.py`): input = the list of already-classified `comparison_changes` (change_type, severity, section, old_text/new_text); output = natural-language prose *only* — the prompt explicitly forbids the model from asserting anything not present in the provided change list (mirrors the existing `citation_validator.py::check_entailment()` philosophy of "the LLM narrates/validates already-computed facts, never originates them"). This satisfies Backend §41's explicit rule verbatim: "this narration LLM call does not itself decide what changed or how severe it is... only *phrasing* is generative."

**Citations for the narrated answer:** reuse the existing atomic message+citations persistence path completely unmodified (§9.9 point 2) — one `Citation` row per change the narration references, `chunk_id` = that change's `old_chunk_id` or `new_chunk_id` (whichever side the narrated sentence is about; if both are referenced in one sentence, two citation rows, same as any multi-source RAG answer already produces).

**Files:**
- `app/rag/query_analyzer.py` — **MODIFY**: extend temporal-scope extraction to optionally detect a second date reference (small, additive).
- `app/services/comparison_service.py` — **MODIFY** (adds to Task 6's file): add `resolve_comparison_targets_from_chat`.
- `app/services/ask_service.py`, `app/services/chat_service.py` — **MODIFY**: add the intent branch above.
- `app/rag/comparison_narration.py` — **CREATE**: `narrate_changes()`.

**Dependencies:** Task 6, Task 9.

**Acceptance criteria:** §16.2 integration tests — "what changed?" while exactly 2 documents are in conversation scope produces a comparison-backed, citation-bearing answer; the same question with 0 or 1 or 3+ documents in scope produces the clarifying fallback, never a fabricated answer; re-asking after the comparison completes returns the persisted result (no recomputation, verified via a call-count assertion on the stub LLM/worker).

---

## 15. Frontend Changes

### Task 13 — Reconstruct `frontend/src/lib` (BLOCKING PREREQUISITE — see Gap 6, §4.5)

**Files (all CREATE — none exist):**
- `frontend/src/lib/api/client.ts` — axios instance; base URL from Vite's `/api` proxy (already configured in `vite.config.ts` — confirm and reuse, do not change); request interceptor attaching `Authorization: Bearer <token>` from `tokenStore`; response interceptor handling 401 → single-flight refresh (mirror `authStore.ts::refreshAccessToken`'s existing dedupe pattern) → retry once → on second failure, call `setSessionExpiredHandler`'s registered callback (already wired in `App.tsx`, confirm exact expected signature by reading `App.tsx`'s usage before writing this file).
- `frontend/src/lib/api/auth.ts`, `frontend/src/lib/api/documents.ts`, `frontend/src/lib/api/chat.ts`, `frontend/src/lib/api/ask.ts` — reconstruct exactly the function names/signatures/types already referenced by existing call sites (`loginApi`, `getDocumentApi`, `listDocumentsApi`, `getDocumentPagesApi`, `getDocumentTocApi`, `getDocumentChunksApi`, `getDownloadUrlApi`, `getDocumentStatusApi`, `listProcessingJobsApi`, `retryProcessingApi`, `streamChatMessage`, etc. — grep every existing `hooks/queries/*.ts` and `features/*/*.tsx` file for `from '@/lib/api/...'` imports and implement exactly what is imported, with exactly the types already used at each call site, per the `<verb><Noun>Api` naming convention already established).
- `frontend/src/lib/api/comparison.ts` — **CREATE new** (Phase 12 addition, not reconstruction): `createComparisonApi`, `getComparisonApi`, `getComparisonChangesApi`, matching §13's actual backend contract (not the Frontend doc's differing example).
- `frontend/src/lib/api/versions.ts` — **CREATE new**: `listDocumentVersionsApi` (wraps `GET /documents/{id}/versions`, now enriched with `state`, §11 Task 2).
- `frontend/src/lib/query/client.ts` — `QueryClient` instance with sane defaults (retry: 2 for network errors, not for 4xx — matching the Frontend doc §10.3 cross-cutting rule already documented).
- `frontend/src/lib/realtime.ts` — `consumeSseStream(url, {method, signal, onEvent})` — reconstruct per the exact usage pattern already visible in `useDocumentProcessing.ts`/`useConversations.ts` (EventSource-style or fetch-based SSE reader; check whichever approach is consistent with `POST`-body SSE needs of the chat streaming endpoint, since `EventSource` cannot send a POST body — likely a `fetch` + `ReadableStream` reader implementation).

**Acceptance criteria:** `npm run build` (or `vite build`) succeeds with zero unresolved-import errors; every existing hook/component that previously imported from the missing `lib/` now resolves correctly; manually verify (via the `run` skill / dev server) that login, document workspace, and ask-page flows still work exactly as before — this task must not change any existing behavior, only make the already-assumed contracts real.

### Task 14 — Version selector/history in Document Workspace

**Files:**
- `frontend/src/hooks/queries/useDocumentVersions.ts` — **CREATE**: `useDocumentVersions(documentId)` (React Query hook wrapping `listDocumentVersionsApi`, key `['documents', documentId, 'versions']`).
- `frontend/src/features/documents/VersionSelector.tsx` — **CREATE**: dropdown/list showing all versions with their `state` (CURRENT/SUPERSEDED/SCHEDULED badge), `version_label`/`version_number`, `effective_date`. Selecting a non-current version updates the workspace's active version (via a URL search param, e.g. `?version=<number>`, consistent with the existing `?page=&q=` URL-driven-state convention already used in `DocumentWorkspace.tsx`).
- `frontend/src/features/documents/DocumentWorkspace.tsx` — **MODIFY**: read the `version` search param (already sent by `CitationBadge.tsx`/`CitationList.tsx` but previously dropped — this closes that dead-parameter gap); when a non-current version is selected/linked-to, pass its `version_id` through to `useDocumentPages`/`useDocumentToc`/`useSignedUrl` (these hooks currently only operate on "current" implicitly — extend their query functions to accept an optional `version` param and forward it to the corresponding backend endpoints, all of which already support `?version=` per `GET /documents/{id}/download?version=`'s existing precedent; confirm during implementation whether `GET /documents/{id}/pages`, `/toc`, `/chunks` already accept a `version` query param on the backend — if not, add it there too, following the exact pattern `get_download_url` already uses); render the "You are viewing v{N} — [View latest]" banner per Frontend §6.5 when the active version isn't `CURRENT`.

**Dependencies:** Task 13, §11 Task 2 (backend `state` field).

**Acceptance criteria:** manually verified in-browser (via the `run` skill): switching versions in the selector updates the TOC/pages panel to that version's content; the non-current banner appears/disappears correctly; a citation link carrying `?version=N` correctly opens the workspace at that historical version, not silently defaulting to current.

### Task 15 — Comparison UI

**Files (all CREATE):**
- `frontend/src/features/compare/index.ts` — barrel.
- `frontend/src/features/compare/ComparisonPicker.tsx` — the `/app/compare` page: two lightweight document/version selectors (reuses `listDocumentsApi` for the document dropdown — a simple `<select>`/combobox, **not** a rebuild of the full Documents table — plus `useDocumentVersions` for the version sub-selector once a document is chosen), a "Compare →" button calling `createComparisonApi`, then navigating to `/app/compare/:comparisonId`.
- `frontend/src/features/compare/ComparisonResults.tsx` — the `/app/compare/:comparisonId` page: polls `GET /compare/:id` (reuse the exact SSE-with-polling-fallback pattern from `useDocumentStatus`, §7 of the frontend research — `JOB_TYPE_LABELS.COMPARISON` already exists in `processingStatus.ts`, confirm it renders sensibly for this staged-processing indicator) until `status === 'COMPLETED'` or `'FAILED'`, then renders:
  - `ComparisonSummary.tsx` — severity-breakdown card (Frontend §6.10 mockup: "N changes detected, X Major · Y Moderate · Z Minor").
  - `SectionNavigator.tsx` — left-panel section list with per-section change-density indicator.
  - `DiffViewer.tsx` — the three view modes (Split/Unified/List) per Frontend §14's responsive table (≥1024px: Split default, user-switchable; 768–1023px: Unified default; <768px: List-only) — implement the viewport breakpoints with a simple `matchMedia`/resize-listener hook, no new dependency needed.
  - `ChangeCard.tsx` — collapsed/expanded change detail with old/new text, word-level diff highlighting inside `MODIFIED` blocks (compute the word-level highlight client-side from `old_text`/`new_text` using the same normalization approach as the backend's `difflib`-based diff — a lightweight client-side word diff is fine here, e.g. a small manual LCS or a minimal diffing routine; do not add a new heavy diff library for this alone unless one is already a dependency — check `package.json` first), severity badge, "View Sources" (navigates to the Document Workspace at the old/new source's page — reusing the exact `navigate(...?page=&q=&version=)` pattern from `CitationBadge.tsx`, opened twice — once per side, or as two links).
  - `ChangeSeverityBadge.tsx` — small reusable badge component.
- `frontend/src/hooks/queries/useComparisons.ts` — **CREATE**: `useCreateComparison()` (mutation), `useComparison(id)` (query + poll-until-terminal, mirroring `useDocumentStatus`'s pattern), `useComparisonChanges(id, {severity, section})`.
- `frontend/src/App.tsx` — **MODIFY**: replace the two `PlaceholderPage` routes (lines 198-199) with `<ComparisonPicker />` and `<ComparisonResults />`.
- `frontend/src/features/documents/DocumentWorkspace.tsx` — **MODIFY**: wire the existing "Compare" header action (Frontend §6.5 mockup shows it; confirm/add if not already present in the actual component) to navigate to `/app/compare?documentA=<id>` pre-filling Document A, per Frontend §6.5's documented behavior.

**Dependencies:** Task 13, Task 14 (for the "View Sources"/"Open in document" navigation target), backend Task 12 (API contract).

**Acceptance criteria:** manually verified end-to-end (via the `run` skill) against the backend fixture from §16.6: create a comparison for the 2025/2026 fixture documents, observe the staged processing indicator, land on results showing the correct severity breakdown, expand the "Approval Timeline" change, click "View Sources" for both old and new, confirm each navigates into the correct document version at the correct page with the correct text highlighted.

---

## 16. Testing Strategy

### 16.1 Unit Tests (`backend/tests/unit/`)

| File | Covers |
|---|---|
| `test_versioning.py` (extend) | §8.4 fix cases (future-dated, overlapping, no-candidate); `validate_effective_window` (valid, invalid, both-null, expiration==effective boundary); `classify_version_state` (CURRENT/SUPERSEDED/SCHEDULED across a 3-version fixture) |
| `test_comparison_rules.py` (new) | `classify_severity` — every row of §9.8's worked-examples table, plus boundary values (`proportion_changed` exactly at 0.3/0.5 thresholds) |
| `test_section_alignment.py` (new) | exact-number match; normalized-title fallback; embedding-similarity fallback (using `StubEmbeddingProvider`); unmatched → ADDED/REMOVED; pathological-restructuring threshold (< 30% match rate triggers whole-document fallback) |
| `test_text_diff.py` (new) | content-hash short-circuit; whitespace-only normalization → UNCHANGED; genuine word-level MODIFIED; `proportion_changed` computation correctness |
| `test_authorization_service.py` (new, or extend existing) | §9.2's authorize-single-version matrix |
| `test_comparison_narration_parsing.py` (new) | semantic-comparison and narration prompt output parsing — malformed JSON, missing fields, valid happy path (mirrors `test_query_analyzer.py`'s parser-testing style) |

### 16.2 Integration Tests (`backend/tests/integration/`)

| File | Covers |
|---|---|
| `test_comparison_pipeline.py` (new) | End-to-end worker run against a seeded two-version fixture (extend the existing `_seed_document_with_chunks`-style helper from `test_ask.py` to create **two** versions of one document, `version_number=1` and `=2`, with distinct chunk content and one deliberately-modified sentence); asserts correct `comparison_changes` rows, correct severity, `document_comparisons.status=COMPLETED`, correct `summary` counts |
| `test_comparison_pipeline.py` (same file) | Resume-after-crash: kill the simulated worker after N of M sections persisted, re-run, assert no duplicate `comparison_changes` rows and all M eventually persisted |
| `test_comparison_pipeline.py` (same file) | Semantic-comparison LLM failure (via `StubLLMProvider` raising) does not fail the job; resulting change has `severity` computed via the `materiality=None` branch |
| `test_comparison_pipeline.py` (same file) | Worker retry/backoff on a `RetryableJobError`; exhaustion → `FAILED` + dead-letter (mirror `test_processing_jobs.py`'s existing retry-exhaustion test structure) |
| `test_chat_change_detection.py` (new) | Chat "what changed?" with exactly 2 documents in scope → comparison-backed, citation-bearing answer; with 0/1/3+ documents → clarifying fallback, never a fabricated answer; re-asking after completion serves the persisted result (assert stub-LLM/worker call counts do not increase) |

### 16.3 API Tests (`backend/tests/api/`)

| File | Covers |
|---|---|
| `test_compare.py` (new) | `POST /compare` → `202` new; `200` existing (both `COMPLETED` and still-`PROCESSING` cases); `422` not-READY version; `422` identical version IDs; `403`/`404` per the authorization matrix below; `GET /compare/{id}` → `200`/`404`; `GET /compare/{id}/changes?severity=&section=` filtering correctness; `GET /compare/{id}/changes/{changeId}` → `200`/`404` |

### 16.4 Authorization Tests (part of `test_compare.py`, explicit matrix)

| User can access A | User can access B | Expected |
|---|---|---|
| yes | yes | allowed (creation and all reads succeed) |
| yes | no | `403`/`404` at creation (per §9.2, treat as 404 to avoid leakage — confirm final choice matches the rest of the codebase's convention during implementation, e.g. check whether `document:read` failures elsewhere return 403 or 404 and match it exactly) |
| no | yes | same as above, symmetric |
| no | no | same as above |
| both allowed at creation, A's access revoked before a later GET | — | `GET /compare/{id}` now fails (re-checked every read, §13) — explicit test |

### 16.5 Worker Tests

Covered within `test_comparison_pipeline.py` (§16.2) rather than a separate file, matching this codebase's existing convention of testing worker behavior inside `integration/` alongside the pipeline it's part of (see `test_extraction_pipeline.py`, `test_embedding_pipeline.py` as precedent — there is no separate `test_workers.py` file pattern in this codebase to imitate).

### 16.6 End-to-End Fixture

**New fixture, added to `backend/tests/fixtures/`** (alongside the existing `golden_documents.py`, or a new `comparison_fixtures.py`): "Marketing Policy" with two versions:
- **v1 ("2025")**: `effective_date=2025-01-01`, section "3.1 Approval Process" containing "Approval must be completed within 5 business days."
- **v2 ("2026")**: `effective_date=2026-01-01`, same section renumbered identically, text changed to "Approval must be completed within 7 business days."

**Test** (`test_comparison_pipeline.py`, the primary Phase 12 acceptance test): create both versions (READY, chunked, embedded via `StubEmbeddingProvider`), `POST /compare`, wait for `COMPLETED`, assert:
- Exactly one `MODIFIED` change for section "3.1 Approval Process".
- Its `old_text` contains "5 business days", `new_text` contains "7 business days".
- Severity is `MODERATE` (non-critical section, per §9.8's worked example) or `MAJOR` if the test configures `organization.settings["comparison"]["critical_sections"] = ["Approval Process"]` (test both configurations explicitly, matching the worked-examples table).
- `GET /compare/{id}/changes/{changeId}` resolves `old_source`/`new_source` to the correct page numbers in each version.
- Re-`POST /compare` for the same pair returns `200` with the same `comparison_id`, and no new `processing_jobs` row is created (assert row count unchanged).
- A chat message "What changed between the 2025 and 2026 versions?" (scoped to this one document, with `analysis.temporal_scope` detecting both years) returns a narrated answer mentioning the change, backed by citations pointing at both the old and new chunk.

---

## 17. File-Level Change Map

### Backend

```
File: backend/alembic/versions/012_document_comparisons.py
Action: CREATE
Purpose: Add document_comparisons, comparison_changes tables; extend processing_jobs.
Changes: See §10 Task 1 SQL in full.
Dependencies: none (down_revision=011)

File: backend/alembic/env.py
Action: MODIFY
Purpose: Register new models for autogenerate parity.
Changes: Import DocumentComparison, ComparisonChange alongside existing model imports.
Dependencies: comparison.py model file must exist first.

File: backend/app/models/comparison.py
Action: CREATE
Purpose: SQLAlchemy models for DocumentComparison, ComparisonChange.
Changes: See §10.
Dependencies: none.

File: backend/app/models/processing_job.py
Action: MODIFY
Purpose: Add comparison_id nullable FK + index + CHECK constraint.
Changes: See §10.
Dependencies: comparison.py.

File: backend/app/domain/versioning.py
Action: MODIFY
Purpose: Fix resolve_current_version(); add validate_effective_window(), classify_version_state().
Changes: See §8.4, §8.5, §8.6.
Dependencies: none.

File: backend/app/domain/comparison_rules.py
Action: CREATE
Purpose: classify_severity(), compute_proportion_changed().
Changes: See §9.8.
Dependencies: none.

File: backend/app/domain/section_alignment.py
Action: CREATE
Purpose: align_sections() — pure section-matching per §9.4.
Changes: See §9.4, §11 Task 2.
Dependencies: none.

File: backend/app/domain/text_diff.py
Action: CREATE
Purpose: diff_text() — normalization + span diff per §9.5.
Changes: See §9.5, §11 Task 2.
Dependencies: none.

File: backend/app/repositories/document_comparison_repository.py
Action: CREATE
Purpose: DB access for document_comparisons/comparison_changes.
Changes: See §11 Task 5.
Dependencies: models/comparison.py.

File: backend/app/services/authorization_service.py
Action: MODIFY
Purpose: Extract _check_document_access_level(); add authorize_document_version().
Changes: See §9.2, §11 Task 4.
Dependencies: none.

File: backend/app/services/comparison_service.py
Action: CREATE
Purpose: get_or_create_comparison(), resolve_comparison_targets_from_chat().
Changes: See §9.1-9.9, §11 Task 6, §14.
Dependencies: authorization_service.py, document_comparison_repository.py, job_service.py.

File: backend/app/services/job_service.py
Action: MODIFY
Purpose: create_for_comparison().
Changes: See §11 Task 7.
Dependencies: models/comparison.py.

File: backend/app/services/ask_service.py
Action: MODIFY
Purpose: Branch on COMPARISON/CHANGE_DETECTION intent.
Changes: See §14.
Dependencies: comparison_service.py.

File: backend/app/services/chat_service.py
Action: MODIFY
Purpose: Same branching for the conversational path (delegates to ask_service.py — verify whether the branch belongs in chat_service.py directly or is inherited via delegation; implement in whichever module actually owns the RAG-vs-deferred-intent decision point after reading both files).
Changes: See §14.
Dependencies: ask_service.py changes.

File: backend/app/workers/jobs.py
Action: MODIFY
Purpose: Dispatch branch for COMPARISON jobs; handle_comparison() handler.
Changes: See §12 Task 8.
Dependencies: comparison_service.py, domain/section_alignment.py, domain/text_diff.py, domain/comparison_rules.py.

File: backend/app/api/compare.py
Action: CREATE
Purpose: POST /compare, GET /compare/{id}, GET /compare/{id}/changes[/{changeId}].
Changes: See §13.
Dependencies: comparison_service.py, schemas/comparison.py.

File: backend/app/main.py
Action: MODIFY
Purpose: Register the new compare router.
Changes: app.include_router(compare.router) alongside existing routers.
Dependencies: api/compare.py.

File: backend/app/schemas/comparison.py
Action: CREATE
Purpose: Pydantic request/response schemas for the compare API.
Changes: See §13.
Dependencies: none.

File: backend/app/schemas/document.py
Action: MODIFY
Purpose: Add `state: Literal["CURRENT","SUPERSEDED","SCHEDULED"]` to DocumentVersionDetail/Summary.
Changes: See §11 Task 2 (enrichment of GET /documents/{id}/versions).
Dependencies: domain/versioning.py::classify_version_state.

File: backend/app/api/documents.py
Action: MODIFY
Purpose: Populate the new `state` field in get_versions(); confirm/add expiration_date + version query-param support on pages/toc/chunks endpoints if not already present (§15 Task 14).
Changes: See §11 Task 2, §15 Task 14.
Dependencies: schemas/document.py, domain/versioning.py.

File: backend/app/services/document_service.py
Action: MODIFY
Purpose: Call validate_effective_window() at upload/version-create time; populate `state` for get_version_list().
Changes: See §8.5, §11 Task 2.
Dependencies: domain/versioning.py.

File: backend/app/rag/prompts.py
Action: MODIFY
Purpose: Add semantic-comparison and change-narration prompt templates.
Changes: See §11 Task 9.
Dependencies: none.

File: backend/app/rag/comparison_narration.py
Action: CREATE
Purpose: narrate_changes() — constrained narration LLM call.
Changes: See §14.
Dependencies: rag/prompts.py.
```

### Backend Tests

```
File: backend/tests/unit/test_versioning.py — MODIFY — see §16.1
File: backend/tests/unit/test_comparison_rules.py — CREATE — see §16.1
File: backend/tests/unit/test_section_alignment.py — CREATE — see §16.1
File: backend/tests/unit/test_text_diff.py — CREATE — see §16.1
File: backend/tests/unit/test_authorization_service.py — CREATE — see §16.1
File: backend/tests/unit/test_comparison_narration_parsing.py — CREATE — see §16.1
File: backend/tests/integration/test_comparison_pipeline.py — CREATE — see §16.2, §16.6
File: backend/tests/integration/test_chat_change_detection.py — CREATE — see §16.2
File: backend/tests/integration/test_repositories.py — MODIFY (or new file alongside) — see §11 Task 5
File: backend/tests/integration/test_migrations.py — MODIFY — add CHECK-constraint assertions for the new tables, see §10
File: backend/tests/api/test_compare.py — CREATE — see §16.3, §16.4
File: backend/tests/fixtures/comparison_fixtures.py — CREATE — see §16.6
File: backend/tests/conftest.py — MODIFY — add "comparison_changes", "document_comparisons" to _CLEANUP_TABLES (§4.4 item 8)
```

### Frontend

```
File: frontend/src/lib/api/client.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/auth.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/documents.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/chat.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/ask.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/comparison.ts — CREATE — see §15 Task 13
File: frontend/src/lib/api/versions.ts — CREATE — see §15 Task 13
File: frontend/src/lib/query/client.ts — CREATE — see §15 Task 13
File: frontend/src/lib/realtime.ts — CREATE — see §15 Task 13
File: frontend/src/hooks/queries/useDocumentVersions.ts — CREATE — see §15 Task 14
File: frontend/src/hooks/queries/useComparisons.ts — CREATE — see §15 Task 15
File: frontend/src/hooks/queries/useDocuments.ts — MODIFY — accept optional version param, see §15 Task 14
File: frontend/src/features/documents/VersionSelector.tsx — CREATE — see §15 Task 14
File: frontend/src/features/documents/DocumentWorkspace.tsx — MODIFY — see §15 Task 14, Task 15
File: frontend/src/features/compare/index.ts — CREATE — see §15 Task 15
File: frontend/src/features/compare/ComparisonPicker.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/ComparisonResults.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/ComparisonSummary.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/SectionNavigator.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/DiffViewer.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/ChangeCard.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/ChangeSeverityBadge.tsx — CREATE — see §15 Task 15
File: frontend/src/features/compare/compare.css — CREATE — styling, follow existing ask.css/processing.css conventions
File: frontend/src/App.tsx — MODIFY — replace two PlaceholderPage routes, see §15 Task 15
```

---

## 18. Data Flow

```
User (UI or chat) selects/names Version A + Version B
        │
        ▼
POST /compare  ──or──  ComparisonService.resolve_comparison_targets_from_chat()
        │
        ▼
AuthorizationService.authorize_document_version() × 2   [fail → 403/404, nothing persisted]
        │
        ▼
Normalize pair (document_id, version_number) tuple compare
        │
        ▼
ComparisonRepository.get_by_pair()  ──found──►  return existing (skip to bottom)
        │ not found
        ▼
Both versions READY?  ──no──►  422
        │ yes
        ▼
INSERT document_comparisons (PENDING) + INSERT processing_jobs (COMPARISON, comparison_id=..)
        │
        ▼
commit → enqueue Arq pointer → 202 {comparison_id, status: PENDING}
        │
        ▼ (async, worker process)
handle_comparison(): align sections → diff text → semantic compare (bounded) →
                     classify_severity() → map to old/new chunks → persist per-section
        │
        ▼
document_comparisons.status = COMPLETED, summary = {total, major, moderate, minor}
        │
        ├──► GET /compare/{id} / /changes  →  DiffViewer / ChangeCard  →  View Sources
        │                                     → navigate(/app/documents/:id?page=&q=&version=)
        │
        └──► (if triggered from chat) narrate_changes() → persist Message+Citations →
              SSE stream to the chat UI (existing Phase 11 mechanism, unmodified)
```

---

## 19. Testing Strategy

(Consolidated cross-reference — full detail in §16.)

- **19.1 Unit Tests:** §16.1.
- **19.2 Integration Tests:** §16.2.
- **19.3 API Tests:** §16.3.
- **19.4 Authorization Tests:** §16.4.
- **19.5 Worker Tests:** §16.5.
- **19.6 End-to-End Tests:** §16.6.

---

## 20. Edge Cases

| Edge case | Expected behavior |
|---|---|
| Two versions uploaded simultaneously (same document) | Pre-existing behavior, unmodified by this plan — `UniqueConstraint(document_id, version_number)` is the backstop; a genuine race can raise `IntegrityError` uncaught today. Out of scope to fix in Phase 12 (see §8.1). |
| Version-number race | Same as above. |
| Missing effective date | Valid — `effective_date IS NULL` is a supported state (§8.4 rule 1); such a version is a candidate for "current" at any date once `READY` (ranks lowest in tie-breaks, per rule 2). |
| Future effective version | Never selected as CURRENT before its date arrives (§8.4 fix); classified `SCHEDULED` (§8.6). |
| Overlapping effective periods | Deterministically resolved to one "current" via latest-`effective_date`-then-`created_at` tie-break (§8.4 rule 3); overlap itself is not flagged as an error — that is Phase 13's Conflict Detection concern. |
| No READY version | `resolve_current_version` returns `None`; comparison creation against a not-READY version → `422`. |
| Comparing a version with itself | Rejected `422` before any DB write (§9.3). |
| Comparing versions from different documents | Explicitly supported ("cross-document" per roadmap frontend note); pair-ordering uses the `(document_id, version_number)` tuple rule (Gap 4, §4.5), not `version_number` alone. |
| Unauthorized version (either side) | Whole request fails before any read/write; treated as `404` (§9.2, §13). |
| Deleted document | `documents.deleted_at IS NOT NULL` → `authorize_document_version` treats it as not-found (extend the existing check to also test `deleted_at`, matching `resolve_allowed_documents`'s existing `Document.deleted_at.is_(None)` filter). |
| Deleted version | No version-deletion flow exists anywhere in this codebase today (verified) — not applicable in Phase 12; the `document_comparisons` FK is `RESTRICT` in anticipation of one being added later (§10). |
| Empty document (zero sections) | Alignment produces zero matched/unmatched sections on that side; if the *other* side has content, every one of its sections becomes `ADDED` (or `REMOVED` if the empty side is B); if both are empty, `document_comparisons.summary = {total: 0, ...}`, `status=COMPLETED`, UI shows Frontend §6.10's documented empty state ("No differences detected between these versions"). |
| Identical versions (byte-identical content) | Every chunk's `content_hash` matches → every section `UNCHANGED` → zero `comparison_changes` rows → same empty-state UI as above. |
| Completely different documents (cross-document, unrelated content) | Alignment's <30% match-rate threshold (§9.4) triggers the whole-document fallback; likely nearly everything classifies as `ADDED`/`REMOVED` at the pseudo-section level — this is correct, expected behavior, not an error. |
| Heavily renumbered sections | Handled by the title-normalization and embedding-similarity fallback passes (§9.4 passes 2-3). |
| Renamed sections (same number) | Matched by exact `section_number` (pass 1) — renaming alone never breaks alignment. |
| Moved sections (reordered, unchanged content) | Not treated as a change type at all — position/`sort_order` is never diffed, only content (§9.4). |
| Large documents | Bounded by per-section processing (not whole-document at once); no explicit page/size cap on the number of sections processed — if this becomes a real performance concern in practice, that is a tuning/observability follow-up, not a Phase 12 architectural change. |
| Large sections | Truncated (2,000 tokens/side) for the semantic-comparison LLM call only, disclosed via the `truncated` flag; the deterministic text diff (§9.5) always processes full content, never truncated. |
| Semantic provider failure | Job continues; that section's `materiality=None`, severity falls to the reduced-confidence branch (§9.6, §9.8) — never fails the whole comparison. |
| Semantic provider timeout | Same as failure — the existing `LLMProviderError`/timeout handling in `LLMProvider.generate()` already surfaces this uniformly; no special-casing needed. |
| Worker crash | Resumable via per-section incremental persistence + re-read-already-written-changes-on-retry (§12 Task 8 point 4). |
| Partial comparison (job still PROCESSING) | `GET /compare/{id}` returns `status=PROCESSING` with whatever `comparison_changes` rows exist so far — the API does **not** withhold partial results; `GET /compare/{id}/changes` during processing returns the partial set (UI should show a "still processing" indicator alongside partial results, not block on completion — matches the roadmap's "staged processing indicator" UX intent). |
| Duplicate comparison request (sequential) | Reuse check returns the existing row (§9.3); no new job. |
| Concurrent comparison requests (same pair, simultaneous) | Unique constraint prevents two rows; the losing insert's `IntegrityError` is caught and the service re-fetches via `get_by_pair` instead of propagating a 500 (§11 Task 6). |
| Citation source missing (chunk deleted) | `old_chunk_id`/`new_chunk_id` `SET NULL` on chunk deletion; the denormalized `old_text`/`new_text` snapshot still renders — "View Sources" degrades to showing the snapshot text without a live navigable link (UI: disable/gray out the "Open in document" action when the corresponding chunk FK is `NULL`). |
| Stale comparison after document deletion | No document-hard-delete flow exists today (verified); `document_comparisons`'s `RESTRICT` FK on both version columns means this scenario cannot occur under the current codebase — flagged for whichever future phase adds hard document deletion to explicitly handle existing comparisons (block the delete, or cascade with an explicit archival step) rather than silently orphaning them. |

---

## 21. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Semantic-comparison LLM cost on large documents | Bounded by §9.6's per-section batching (one call per `MODIFIED` section, not per sentence) and the 2,000-token truncation cap. |
| Alignment quality on heavily renumbered/retitled documents | Title-embedding fallback (§9.4 pass 3) plus the disclosed whole-document degradation path (§9.4 failure behavior) — never silently produces a misleading section-level diff. |
| Diff UX scope creep | §15's component list is the contract — `ComparisonSummary`, `SectionNavigator`, `DiffViewer` (3 modes), `ChangeCard`, `ChangeSeverityBadge`. Do not add features beyond this list without a scope discussion. |
| The `frontend/src/lib` reconstruction (Task 13) diverging from an unknown "real" original implementation | Flagged explicitly as `UNKNOWN / REQUIRES DECISION` in Gap 6 (§4.5) — confirm with the team before or during implementation whether a canonical version exists elsewhere; if not, the reconstruction in this plan is functionally sufficient but should be reviewed once real backend responses are available (not just inferred shapes). |
| Severity threshold tuning (§9.8's plan-authored constants: 0.86 embedding threshold, 30% alignment-degradation threshold, 0.3/0.5 proportion thresholds) turning out wrong in practice | All four live in pure, isolated functions (`domain/comparison_rules.py`, `domain/section_alignment.py`) specifically so they can be retuned later without touching the pipeline orchestration around them. |
| `processing_jobs.comparison_id` schema extension (Gap 3) being the wrong long-term shape if Phase 13/14 add more multi-entity job types (e.g., conflict scans spanning many documents) | Acceptable for Phase 12 — do not over-generalize `processing_jobs` preemptively for hypothetical future job shapes; if Phase 13 needs something similar, it can follow the same nullable-FK-per-job-type-family pattern established here. |
| Chat CHANGE_DETECTION blocking the SSE stream on a multi-minute worker job | Explicitly designed around (§14): respond immediately with a "comparing now, ask again shortly" message rather than blocking; do not add synchronous long-polling inside the chat request handler. |

---

## 22. Implementation Order

```
Step 1  — Domain layer fixes (Task 2, 3): resolve_current_version fix, validate_effective_window,
          classify_version_state, comparison_rules.py, section_alignment.py, text_diff.py.
          [No dependencies. Do this first — everything else builds on correct version resolution.]

Step 2  — Database migration (Task 1): document_comparisons, comparison_changes, processing_jobs
          extension, models/comparison.py, alembic/env.py update.
          [Depends on: nothing functionally, but write after Step 1 so column/field names in the
          models match what Step 1's domain functions expect to consume.]

Step 3  — Repository + Authorization (Task 4, 5): document_comparison_repository.py,
          authorize_document_version(), extracted _check_document_access_level().
          [Depends on: Step 2 (models must exist).]

Step 4  — Backend schema enrichment (§11 Task 2): DocumentVersionDetail.state field,
          effective_date validation wired into document_service.py, expiration_date form field
          if missing.
          [Depends on: Step 1.]

Step 5  — ComparisonService + JobService extension (Task 6, 7): get_or_create_comparison(),
          create_for_comparison().
          [Depends on: Steps 2, 3.]

Step 6  — Prompts (Task 9): semantic-comparison + narration prompt templates in rag/prompts.py.
          [No dependencies — can happen in parallel with Steps 3-5.]

Step 7  — Comparison worker (Task 8): run_processing_job dispatch branch, handle_comparison(),
          full pipeline wiring (alignment → diff → semantic → classify → severity → citation
          mapping → incremental persistence).
          [Depends on: Steps 1, 2, 5, 6. This is the largest single task — budget accordingly.]

Step 8  — API layer (Task 12 = §13): compare.py router, comparison.py schemas, main.py registration.
          [Depends on: Step 5 (service must exist to call).]

Step 9  — CHANGE_DETECTION chat integration (§14): query_analyzer.py temporal-scope extension,
          resolve_comparison_targets_from_chat(), ask_service.py/chat_service.py branching,
          comparison_narration.py.
          [Depends on: Steps 5, 7 (needs a working comparison pipeline to narrate results from).]

Step 10 — Backend tests (§16.1-16.4, 16.6): unit tests can start as early as Step 1 completes
          (test each domain function immediately after writing it, not all at the end); integration/
          API/E2E tests require Steps 7-9 complete.
          [Interleave with Steps 1-9 rather than treating as a single final step — write each
          layer's tests alongside that layer, per the existing codebase's evident TDD-adjacent
          convention (every existing module has a corresponding test file).]

Step 11 — Frontend lib reconstruction (Task 13).
          [No backend dependency — can start in parallel with Step 1, but block Steps 12-13 below
          on it.]

Step 12 — Frontend version selector (Task 14).
          [Depends on: Step 11, Step 4 (needs the `state` field in the API response).]

Step 13 — Frontend comparison UI (Task 15).
          [Depends on: Step 11, Step 8 (needs the real API contract), Step 12 (View Sources
          navigates through the version-aware workspace).]

Step 14 — End-to-end validation (§16.6 fixture, manual browser verification per Task 15's
          acceptance criteria).
          [Depends on: everything above.]
```

---

## 23. Definition of Done

Phase 12 is **not** done merely because `POST /compare` returns a `202` and a worker eventually writes rows to `comparison_changes`. It is done when the full explainability chain is demonstrable end-to-end, exactly as the roadmap's Phase 12 Deliverables state:

- [ ] Version A + Version B, named explicitly (never inferred as "previous"), pass through **both-sides authorization** before any comparison work begins or is looked up.
- [ ] Structure alignment correctly matches sections by number → normalized title → embedding similarity, with a disclosed degraded-fallback path for pathological restructuring.
- [ ] Text comparison is fully deterministic and content-hash-short-circuited; semantic comparison is LLM-assisted but strictly additive to severity, never to `change_type`.
- [ ] Every persisted `comparison_changes` row has a correct `change_type` (ADDED/REMOVED/MODIFIED — never UNCHANGED persisted) and a correct, deterministically-computed `severity` (MAJOR/MODERATE/MINOR).
- [ ] Every `MODIFIED`/`ADDED`/`REMOVED` row resolves to real old/new source evidence (page, section, document, version) via the chunk FK join — "View Sources" always shows real text from real pages, never a placeholder.
- [ ] Results are **persisted**, not regenerated on every view; re-requesting the same version pair returns the identical stored result with zero additional worker/LLM invocations.
- [ ] The frontend `/app/compare` flow is fully functional (no `PlaceholderPage` remaining on either comparison route) and demonstrates the exact Journey 4 steps from the Frontend doc (§D.10 in the research, reproduced): select A/B → staged processing → summary with severity breakdown → filter to Major → expand a change → View Sources opens both versions at the exact page → defend "why is this Major" by pointing at real text in both documents.
- [ ] `CHANGE_DETECTION`/`COMPARISON` chat questions route to the comparison service (not silently falling through to generic RAG as they do today), resolve their two target versions **deterministically** (never an LLM guess), and produce a citation-backed narrated answer, or a clear clarifying question when version targets cannot be resolved unambiguously.
- [ ] Historical-scope questions ("what was the policy in 2025?") resolve to the correct version via the fixed `resolve_current_version(as_of=...)`.
- [ ] The `resolve_current_version()` fix (§8.4) is deployed and its regression tests (future-dated version never surfacing as current) pass.
- [ ] The `frontend/src/lib` blocking prerequisite (Task 13) is resolved and the frontend builds cleanly.
- [ ] All tests in §16 pass, including the 2025-vs-2026 end-to-end fixture (§16.6) exactly as the roadmap's stated exit criterion describes.

---

## 24. Phase 12 Acceptance Criteria

(Restating §23 as a compact, checkable list for sign-off — identical substance, terser form for a release-gate checklist.)

1. `alembic upgrade head` reaches revision `012` cleanly on a fresh DB; `downgrade -1` reverses cleanly.
2. `POST /compare` → `202` (new) / `200` (existing) / `422` (not-READY or identical-version) / `403 or 404` (unauthorized either side) — all four paths tested.
3. Full pipeline run on the §16.6 fixture produces exactly the documented `MODIFIED` change with correct severity under both critical/non-critical section configurations.
4. Re-requesting an existing comparison never enqueues a second job (assert via row-count).
5. Worker crash-and-resume never duplicates `comparison_changes` rows.
6. Semantic-provider failure degrades severity computation gracefully without failing the job.
7. Chat "what changed" resolves versions deterministically; ambiguous scope produces a clarifying response, never a guess.
8. Frontend `/app/compare` and `/app/compare/:id` are fully functional, no placeholders remain.
9. Frontend `frontend/src/lib` exists and the app builds without unresolved-import errors.
10. Document Workspace version selector/history and the previously-dead `?version=` citation param both work end-to-end.
11. All new backend unit/integration/API tests (§16, §17) pass; existing test suite remains green (no regressions to Phase 3–11 tests, in particular `test_versioning.py`'s expanded cases and any test that indirectly depended on the old, buggy `resolve_current_version` fallback behavior — audit for this specifically during Step 1's implementation).

---

## 25. GLM 5.3 Flash Implementation Guidance

- **Read order:** implement in the exact sequence of §22 (Implementation Order). Do not start the worker pipeline (Step 7) before the domain-layer fix (Step 1) and the migration (Step 2) are both in place and their own tests pass — the pipeline consumes both directly.
- **Do not reinterpret table/field names.** Use `document_comparisons`/`comparison_changes` exactly as specified in §10, not "comparisons". Use `document_a_version_id`/`document_b_version_id` exactly, not `versionA`/`documentAId` (that shape belongs to the Frontend doc's example, which this plan explicitly overrides — see Gap 2, §4.5).
- **Do not invent an `is_current` column.** The fix in §8.4 is a code fix in `domain/versioning.py`, not a schema addition — the Database Architecture doc explicitly rejects this column for good reasons (trigger complexity, integrity risk). If you find yourself wanting to add `is_current` to `document_versions`, stop and re-read §8.4.
- **Every severity/alignment threshold in §9.4/§9.6/§9.8 (0.86, 30%, 2000 tokens, 0.3, 0.5) is this plan's own proposal**, not an extracted requirement from any source document. Implement them as named constants in `domain/comparison_rules.py`/`domain/section_alignment.py` so they are trivially discoverable and tunable — do not scatter magic numbers inline.
- **Never let the LLM decide `change_type` or persist a change that the deterministic diff did not already detect.** The semantic-comparison call (§9.6) may only ever influence the `severity` computation's `semantic_materiality` input. If your implementation has any code path where an LLM response can create a `comparison_changes` row or alter `change_type`, that is a bug relative to this plan — the task's own instructions are explicit and non-negotiable on this point.
- **Authorization is checked on both sides, every time**, at creation *and* at every subsequent read (§13) — not cached, not skipped on the "fast path" of an already-computed comparison. A comparison that exists is not automatically visible to everyone who knows its ID.
- **The `frontend/src/lib` reconstruction (Task 13) is a prerequisite, not an afterthought** — do it before any other frontend task, and treat "the app builds and every pre-existing page still works exactly as before" as that task's acceptance bar before moving on to anything Phase-12-specific in the frontend.
- **Where this plan says `UNKNOWN / REQUIRES DECISION`** (Gap 6, §4.5), that is a genuine open question for the team, not something to silently resolve on your own judgment — implement the stated fallback (reconstruct from call-site inference) but flag it in your own implementation notes/PR description so a human reviews that specific decision.
- **Do not expand scope.** If you find yourself building a full Documents list/table page, TOC conflict markers, a `SummaryService`, PDF bounding-box highlighting, or any new infrastructure component, stop — none of these are Phase 12 (§5's explicit exclusions).
- **When in doubt about an existing convention** (file naming, schema naming, response shape, test organization), grep the existing codebase for the nearest analogous feature (e.g., how `document_pages`/`DocumentPageRepository` is structured for a new `document_comparisons`/`DocumentComparisonRepository`) and match it exactly rather than introducing a new pattern.
