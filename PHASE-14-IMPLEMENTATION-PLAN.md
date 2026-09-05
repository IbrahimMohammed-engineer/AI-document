# Phase 14 — Advanced Document Intelligence
## Implementation Plan (Database + Backend + Frontend)

**Document type:** Implementation-ready, codebase-aware delta plan
**Audience:** GLM (implementing coding agent), backend/frontend engineers, reviewers
**Authoritative references:**
- `Documentation/AI-Document-Intelligence-Platform-Implementation-Roadmap.md` (Phase 14 section, lines 2177–2303)
- `Documentation/Backend-Architecture-Documentation.md` (§27 Query Understanding, §43 Document Summarization)
- `Documentation/Database-Architecture-Design-Documentation.md` (§8, §13–22, §27–28)
- `Documentation/Frontend-Design-Documentation.md` (§6.5, §6.12 Document Summary, §6.14 Analytics)
- Sibling implementation plans in this repo: `PHASE-12-IMPLEMENTATION-PLAN.md`, `PHASE-13-IMPLEMENTATION-PLAN.md` — this document follows their exact conventions (layering, static-method services, JSONB-vs-child-table reasoning, migration style, GLM guidance) rather than inventing new ones.

> This plan is a **delta** against the actual repository state as of this writing (inspected directly, not assumed from documentation). Every section below distinguishes **already implemented**, **prerequisite gap**, and **new Phase 14 work**. Do not re-derive architecture that already exists — reuse it by direct reference.

---

## 1. Phase 14 Objective

Broaden the platform from a RAG chatbot into a document-intelligence system by delivering three independently-shippable capabilities, all sharing the platform's one citation/validation/persistence discipline:

1. **Cited, structured document summaries** — `SummaryService` produces a schema-constrained summary (executive summary, key points, dates, roles, requirements, risks, topics) per document version, with every factual item resolving to a real, Phase-10-validated citation. Persisted, regenerable, independently owned per version.
2. **Structured-information extraction** — a retrieval-fed, schema-constrained extraction workflow over the first documented schema set (requirements, risks, dates, parties), citation-validated per item, persisted per run (audit trail, not overwrite-in-place).
3. **Six-intent query classification and routing** — completing the dispatcher already scaffolded in `ChatService`/`rag/query_analyzer.py` so `SUMMARY` and `EXTRACTION` route to their new services exactly as `COMPARISON`/`CHANGE_DETECTION` (Phase 12) and `CONFLICT_DETECTION` (Phase 13) already do.

Every new capability reuses, verbatim, the existing citation generation (`rag/citations.py`), citation validation (`rag/citation_validator.py`), context assembly (`rag/context_builder.py`), retrieval (`rag/retriever.py`, `DocumentChunkRepository.semantic_search`), LLM provider abstraction (`infrastructure/llm.py`), background-job machinery (`ProcessingJob` + `JobService` + Arq workers), and authorization/tenant-isolation patterns (`AuthorizationService`, denormalized `organization_id`) established in Phases 1–13. No new infrastructure, no new queue, no new vector store, no microservice.

---

## 2. Current Implementation Assessment

This section is the product of direct repository inspection (not an assumption from the docs). Paths are repo-relative from `backend/` unless noted.

### 2.1 What already exists and Phase 14 must reuse, unchanged

| Area | File(s) | Status |
|---|---|---|
| Query intent enum + classifier | `app/rag/query_analyzer.py` | **Already implements all six intents.** `QueryIntent`/`VALID_INTENTS` already include `QUESTION, COMPARISON, CHANGE_DETECTION, SUMMARY, CONFLICT_DETECTION, EXTRACTION`. `DEFERRED_INTENTS = frozenset({"SUMMARY", "EXTRACTION"})` is the **only** thing left undone — it currently causes these two intents to log-and-fall-through to standard RAG (Backend §51 graceful degradation). Phase 14's routing work is to remove this frozenset's members once real services exist, **not** to build the classifier. |
| Job type enum | `app/domain/state_machines.py` | `JobType.SUMMARY = "SUMMARY"` is **already declared**, with a comment `# Phase 14`, and already has a `get_max_attempts` entry (3). No enum work needed for summaries. `JobType.EXTRACTION` **already exists** but means the Phase 5 ingestion text-extraction stage — see the naming conflict flagged in §2.4 below; a **new** enum value is required for structured extraction. |
| DB check constraint | `processing_jobs.job_type CHECK (...)` (migration `005_processing_jobs.py`, extended by `012`) | Already includes `'SUMMARY'` in the allowed values. Does **not** include a value for structured extraction. |
| Citation generation | `app/rag/citations.py` (`resolve_citations`, `find_quoted_span`, `split_sentences`, `extract_references`) | Complete, pure, reusable verbatim. |
| Citation validation | `app/rag/citation_validator.py` (`validate_answer`, `extract_claims`, `check_entailment`, `find_central_regenerate_reason`) | Complete, reusable verbatim. Operates on **any** answer text + `ContextBundle` — not chat-specific. This is the exact mechanism Phase 14 must drive for summary/extraction claims. |
| Context assembly | `app/rag/context_builder.py` (`build_context`, `ContextBundle`, `SourceBlock`, `dedup_sources`) | Complete, reusable verbatim. Takes `list[SearchResult]` — Phase 14 needs small adapter functions (not new assembly logic) to feed it chunk rows that didn't come from the retriever. |
| Retrieval | `app/rag/retriever.py` (`SemanticRetriever`), `app/rag/hybrid_search.py` (`HybridRetriever`), `app/repositories/document_chunk_repository.py` (`semantic_search`, `keyword_search`, `list_for_version`, `list_for_section`) | Complete. The retrieval-level tenant/version enforcement (`organization_id`, `document_version_id = ANY(...)`) is already the mandatory-predicate pattern Phase 14 must reuse identically. |
| LLM provider abstraction | `app/infrastructure/llm.py` (`get_llm_provider`, `LLMMessage`, `LLMProvider`, `LLMProviderError`) | Complete, reusable verbatim — no new provider code. |
| Background job machinery | `app/models/processing_job.py`, `app/services/job_service.py`, `app/workers/jobs.py`, `app/infrastructure/queue.py` | Complete generic job lifecycle (PENDING→PROCESSING→COMPLETED/FAILED/RETRYING), atomic-row-then-enqueue-after-commit pattern, reconciliation sweep. Phase 14 adds two new "special dispatch" job types (see §2.3's critical finding) exactly like `COMPARISON` and `CONFLICT_SCAN` already did. |
| Comparison/Conflict domain services (patterns to mirror) | `app/services/comparison_service.py`, `app/services/conflict_service.py` | Complete, and the **direct structural template** for `SummaryService`/`ExtractionService`: static methods, explicit `db: AsyncSession` parameter, `get_or_create_*` idempotent creation, chat-intent target resolution helpers, chunk-provenance resolution. |
| Chat intent dispatcher | `app/services/chat_service.py::ChatService.message_stream` | **Already routes** `COMPARISON`/`CHANGE_DETECTION` → `ComparisonService`, `CONFLICT_DETECTION` → `ConflictService`, inside a `try/except` block that falls through to standard `AskService.ask_stream` on any error or unmatched intent (this fallthrough is **also** the mechanism that already satisfies "misclassification degrades gracefully" — Phase 14 adds two more `elif` branches to the *same* block, following the identical shape). |
| Authorization | `app/services/authorization_service.py` (`authorize_document_version`, `resolve_allowed_documents`, `check_permission`) | Complete, reusable verbatim for version-level auth (404-not-403 non-leaking pattern) and permission-key checks. |
| Permission catalog + seeding pattern | `app/domain/permissions.py` (`PermissionKey`), migration `002_identity_tenancy.py` (`_seed_permissions`/`_seed_system_roles`), migration `013_conflicts.py` (adds `conflict:resolve` the same way) | Establishes the exact seed-a-new-permission-in-its-own-phase-migration pattern Phase 14 must follow for two new permission keys. |
| Audit action catalog | `app/services/audit_logger.py` (`AuditAction`) | Fixed catalog; Phase 14 adds new members following the existing naming convention. |
| Prompt module conventions | `app/rag/prompts.py` | Every prompt family follows `<NAME>_PROMPT_VERSION` / `<NAME>_SYSTEM_PROMPT` / `<NAME>_USER_TEMPLATE`. Phase 14 adds `SUMMARY_*` and `EXTRACTION_*` triples the same way. |
| Section/page/chunk provenance model | `app/models/document.py` (`DocumentSection.start_page`/`end_page`, `DocumentChunk.section_id`/`page_id`/`chunk_index`) | Already carries everything needed for section-diverse sampling (§5.6) — no new columns on these tables. |

### 2.2 What is genuinely missing (Phase 14 must build)

- No `document_summaries` table, model, repository, service, API router, schemas, or frontend screen exist anywhere in the repository. Confirmed via `Glob`/`Grep` across `app/models`, `app/repositories`, `app/services`, `app/api`, `app/schemas`, `frontend/src` — zero matches for "summary"/"summaries" outside comments/config keys unrelated to this feature.
- No structured-extraction table, model, repository, service, API router, schemas, or frontend screen exist. Zero matches for a Phase-14-shaped "extraction" concept (the only `EXTRACTION` hits are the Phase 5 ingestion stage — `app/ingestion/extractor.py`, `app/ingestion/chunking_stage.py` references, `handle_extraction` in `workers/jobs.py`, and the `JobType.EXTRACTION` enum value — all unrelated to structured-information extraction).
- No `rag/summary_builder.py`, `rag/extraction_builder.py`, `rag/summary_narration.py`, `rag/extraction_narration.py` — need to be created, mirroring `rag/comparison_narration.py` / `rag/conflict_narration.py` / `rag/conflict_parsing.py`.
- No `domain/summary_rules.py` (section-diverse sampling algorithm) or `domain/extraction_rules.py` (category query constants, item bounds) — need to be created, mirroring `domain/comparison_rules.py` / `domain/conflict_rules.py`.
- `ANALYTICS_READ` permission and the `/analytics` route already exist (permission key seeded; route is a `PlaceholderPage` in `App.tsx` line 237: `"Usage metrics, processing stats, AI quality — Phase 10"`). No analytics endpoint/service exists yet. Phase 14's analytics groundwork is explicitly scoped as "begins accumulating metrics" (roadmap) — see §2.6 scope decision.

### 2.3 Critical finding — the job-dispatch trap (must read before implementing workers)

`backend/app/workers/jobs.py::run_processing_job` has **two distinct dispatch paths**, not one:

1. **Per-version pipeline path** (the `HANDLERS` dict, `EXTRACTION`/`CHUNKING`/`EMBEDDING`/`INDEXING`): loads `document_versions` by `job.document_version_id`, and if `version.status == READY` **already**, it short-circuits at line ~1154 with `mark_completed` **without ever calling the handler** — this is correct for ingestion stages (nothing to do if already READY) but would silently no-op every `SUMMARY`/structured-extraction job forever, because those jobs' whole purpose is to run against an *already-READY* version.
2. **Special dispatch path** (`COMPARISON` → `_run_comparison_job`, `CONFLICT_SCAN` → `_run_conflict_scan_job`): an early-return check *before* the version-status logic, calling a handler with a completely different signature (`(ctx, job, <domain_row>, session)` instead of `(ctx, job, version, document, session)`), and marking completion on the domain row (`document_comparisons.status`) rather than `document_versions.status`.

**`SUMMARY` and the new `STRUCTURED_EXTRACTION` job type belong on the special dispatch path, exactly like `COMPARISON`.** Full detail and exact insertion points are in §5.4.

### 2.4 Discrepancy #1 — `EXTRACTION` name collision (flag + smallest correction)

The roadmap's six intents include one named `EXTRACTION` (structured-information extraction). The codebase **already uses the literal string `"EXTRACTION"`** as a `processing_jobs.job_type` value (`app/domain/state_machines.py::JobType.EXTRACTION`, `handle_extraction` in `workers/jobs.py`), meaning the Phase 5 ingestion **text**-extraction pipeline stage (PDF/DOCX → raw page text). These are unrelated concepts that happen to share a name.

**Smallest correction:** introduce a **new**, distinctly-named job type for Phase 14: `JobType.STRUCTURED_EXTRACTION = "STRUCTURED_EXTRACTION"`. Leave `JobType.EXTRACTION` and everything that depends on it completely untouched. The query-intent literal `"EXTRACTION"` in `rag/query_analyzer.py` is unaffected — it is a classification label, never a `job_type` value, and no code path currently confuses the two. Do not rename anything that already exists.

### 2.5 Discrepancy #2 — `frontend/src/lib` directory does not exist

Twenty-seven frontend files (`useComparisons.ts`, `useConflicts.ts`, `useDocuments.ts`, `DocumentWorkspace.tsx`, `ComparisonPage.tsx`, `App.tsx`, etc.) import from paths like `@/lib/api/comparison`, `@/lib/api/documents`, `@/lib/api/versions`. Direct filesystem inspection (`find frontend/src -maxdepth 4`) confirms **no `frontend/src/lib` directory exists at all** in this checkout. Every file in the tree carries an identical modification timestamp, consistent with a bulk export/copy rather than incremental development — so this may be an artifact of how this checkout was produced rather than a real gap in the maintained repository.

**Recommendation (smallest correction, and a prerequisite, not Phase-14-specific):** before starting any Phase 14 frontend work, confirm against the team's actual working tree whether `frontend/src/lib/api/*` exists. If it genuinely does not, the entire frontend is currently non-compiling and **recreating `lib/api/{documents,versions,comparison,conflicts,conversations,ask}.ts`** (thin `axios`-based fetch wrappers matching the response shapes already defined in `backend/app/schemas/*`) is a blocking prerequisite for *any* frontend work, not just Phase 14's. This plan's frontend section (§6) is written assuming that layer exists or will be restored, and adds `lib/api/summary.ts` / `lib/api/extraction.ts` alongside it using the identical `axios` + typed-response convention visible in `useComparisons.ts`.

### 2.6 Scope decisions made explicit (so GLM does not have to decide these)

The roadmap's Phase 14 prose leaves several points genuinely open. Per the instruction to leave no business-rule decision to the implementing agent, this plan settles them here, once, and the rest of the document assumes these answers:

1. **Extraction scope is single-document, single-version, V1.** The roadmap says "over a document or scope"; this plan implements **document-version scope only** (mirrors summary; mirrors the DB's `document_version_id NOT NULL` job-anchor requirement for every job type except `CONFLICT_SCAN`). Knowledge-base-wide extraction is out of scope and not stubbed.
2. **Extraction schema is fixed in V1**, not organization-configurable, despite the roadmap's "(first schema set, org-configurable later)" aside explicitly deferring configurability. The API still accepts a `schema_key` field (validated as a closed Pydantic `Literal`, currently one value: `"standard_v1"`) so the contract is forward-compatible without building configuration UI/storage now.
3. **Summary persistence is one row per `document_version_id` (UNIQUE), updated in place on regeneration** — mirrors the roadmap's explicit "(document_version_id)" persistence key and "mirroring comparison's persisted-not-cached decision." **Extraction persistence is append-only, one row per run** (`document_extractions`), because the roadmap explicitly says "results persisted per run (linkable/auditable like summaries)" — a deliberately different persistence shape from summary, justified in §4.
4. **Summary "staleness" is triggered by re-processing the *same* `document_version_id`'s chunks**, which today can only happen via `JobService.retry_failed_stage` re-running `CHUNKING`/`EMBEDDING` for a version that had previously reached `FAILED` after already having a summary (an edge case, not the common path — new document versions get independent summaries and never mark an old version's summary stale). No "stale" concept exists for extraction runs (each run is an immutable historical record by design).
5. **Chat-triggered `EXTRACTION` reuses the latest `COMPLETED` run** for the resolved document version rather than creating a new run on every incidental question (cost control); the explicit `POST /extractions` UI action always creates a fresh run (the audit-trail feature the roadmap asks for). This asymmetry is deliberate — see §5.9.
6. **Chat narration for `SUMMARY` requires no second LLM call** — the persisted summary is already prose; chat narration is a deterministic re-render of the stored, already-validated content. Chat narration for `EXTRACTION` **does** call the LLM once (to synthesize prose across up to four categories of already-validated items), mirroring `comparison_narration.py`/`conflict_narration.py`'s "narrate, never originate" pattern.
7. **Section-diverse long-document sampling is computed from existing structural data (`DocumentSection.start_page/end_page`, `DocumentChunk.section_id`/`chunk_index`), not a new embedding-based centrality search.** The roadmap's prose ("most-central chunk(s) via `rag/retriever.py`") is under-specified and would add cost/latency/non-determinism for no proven benefit; §5.6 documents the exact deterministic algorithm to implement instead. This is this plan's own proposal (like PHASE-13's documented numeric thresholds) — implement it as specified, not re-derived.
8. **Analytics groundwork is deliberately minimal in Phase 14.** The roadmap says Phase 14 "begins accumulating" metrics and Phase 19 "formalizes the metrics platform." This plan adds one minimal read-only endpoint over already-existing columns (`messages.groundedness`, `citations` counts, `processing_jobs` status) and does **not** build the full FE §6.14 Analytics screen (charts, cost breakdown, CSV export) — that is explicitly Phase 15/19 scope per the roadmap's own phase boundaries. See §6.7.

---

## 3. Prerequisites and Dependencies

**Hard prerequisites (per roadmap: "Prerequisites: Phase 12–13"):**
- Phase 12 (`ComparisonService`, `document_comparisons`/`comparison_changes`, migration `012`) — **present and complete** in this repository.
- Phase 13 (`ConflictService`, `conflicts`/`conflict_statements`, migration `013`) — **present and complete** in this repository.
- Phase 9–10 (query analyzer, retrieval, context assembly, citation generation/validation) — **present and complete**.
- Phase 11 (conversations, `ChatService`, streaming, `MessageFeedback`) — **present and complete**.

**Soft prerequisite / recommended pre-check (Task 0, mirroring PHASE-13's own Task 0 convention):** before writing any Phase 14 code, run the existing test suite (`pytest backend/tests`) against a clean environment to confirm Phases 0–13 actually pass in the working copy, and resolve §2.5's `frontend/src/lib` question. If either check fails, stop and fix it first — Phase 14 code assumes both are true.

**No new external dependencies.** No new Python packages, no new npm packages beyond what `lib/api/*` already needs (axios — already in `package.json`), no new infrastructure (confirmed: PostgreSQL+pgvector, Redis, object storage, Arq workers are unchanged).

---

## 4. Database Implementation

### 4.1 Design rationale — why two different persistence shapes

`document_summaries` stores its structured content as **one JSONB blob per row** (like `document_comparisons.summary`), while `document_extractions`/`document_extraction_items` uses a **parent + real child rows** shape (like `document_comparisons`/`comparison_changes` and `conflicts`/`conflict_statements`). This mirrors the exact reasoning already documented in this codebase for `document_chunks.embedding` living directly on the chunk instead of a child table ("no query benefit — only extra join overhead"): a summary is always fetched and rendered as one cohesive document, never filtered/sorted/paginated at the item level, so a child table would add join cost for zero benefit. Extraction items, by contrast, are exactly the "diff/statement"-shaped data this schema family already models as real rows (`comparison_changes`, `conflict_statements`) because the UI needs to filter by category, paginate, and link to individual items — the same query patterns that justified those two existing child tables.

### 4.2 New table: `document_summaries`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `organization_id` | `uuid` FK → `organizations(id)` ON DELETE RESTRICT, NOT NULL | Denormalized — every list/status query filters on it directly, mirroring `document_chunks.organization_id`'s stated rationale. |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE RESTRICT, NOT NULL | Denormalized for "summary status for this document" lookups without a join through `document_versions`. |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE RESTRICT, NOT NULL, **UNIQUE** | One row per version — regeneration updates this row in place; a new document version gets its own independent row. |
| `status` | `text` NOT NULL DEFAULT `'PENDING'` | `PENDING` / `PROCESSING` / `COMPLETED` / `FAILED` — identical vocabulary to `document_comparisons.status`. |
| `summary` | `jsonb` NULL | The full structured payload (see §5.7 for exact shape) including inline resolved-citation objects per item. NULL until first `COMPLETED`; **not cleared** on a subsequent regeneration until the new result actually completes (Backend §43 "stale summary remains visible with a warning" — FE §6.12). |
| `sampling` | `jsonb` NULL | Disclosure object: `{"sampled": bool, "strategy": "full"\|"section_diverse", "included_section_ids": [...], "excluded_section_count": int}`. |
| `model` | `text` NULL | LLM model used for the last successful generation (auditability — FE §6.12 subtitle). |
| `prompt_version` | `text` NULL | Value of `SUMMARY_PROMPT_VERSION` used. |
| `stale` | `boolean` NOT NULL DEFAULT `false` | Set `true` only by the re-chunking/re-embedding invalidation hook (§4.6); cleared on the next successful regeneration. |
| `error_message` | `text` NULL | Populated when `status = 'FAILED'`. |
| `requested_by` | `uuid` FK → `users(id)` ON DELETE RESTRICT, NOT NULL | The user whose action (first view or explicit regenerate) most recently (re)triggered generation. |
| `created_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `completed_at` | `timestamptz` NULL | |

**Constraints:** `CHECK status IN ('PENDING','PROCESSING','COMPLETED','FAILED')` (`ck_document_summaries_status`); `UNIQUE (document_version_id)` (`uq_document_summaries_version`).

**Indexes:** `ix_document_summaries_org_status (organization_id, status)`; `ix_document_summaries_document_id (document_id)`.

**Tenant isolation:** every read/list query filters `organization_id = :org_id` as a mandatory predicate (mirrors every other tenant-owned table in this schema); every read additionally re-authorizes the underlying document/version via `AuthorizationService.authorize_document_version` (mirrors `compare.py`'s `_get_authorized_comparison` pattern) so an `access_level` change on the document after summary creation is respected on every subsequent read, not just at creation time.

### 4.3 New table: `document_extractions` (extraction run header)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` ON DELETE RESTRICT, NOT NULL | |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE RESTRICT, NOT NULL | |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE RESTRICT, NOT NULL | **Not unique** — multiple runs per version are the point (audit history). |
| `schema_key` | `text` NOT NULL DEFAULT `'standard_v1'` | `CHECK schema_key IN ('standard_v1')` — closed enum-of-one in V1 (§2.6 point 2), forward-compatible column. |
| `status` | `text` NOT NULL DEFAULT `'PENDING'` | `PENDING` / `PROCESSING` / `COMPLETED` / `FAILED`. |
| `model` | `text` NULL | |
| `prompt_version` | `text` NULL | |
| `error_message` | `text` NULL | |
| `requested_by` | `uuid` FK → `users(id)` ON DELETE RESTRICT, NOT NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT `now()` | |
| `completed_at` | `timestamptz` NULL | |

**Constraints:** `CHECK status IN ('PENDING','PROCESSING','COMPLETED','FAILED')` (`ck_document_extractions_status`); `CHECK schema_key IN ('standard_v1')` (`ck_document_extractions_schema_key`).

**Indexes:** `ix_document_extractions_org_status (organization_id, status)`; `ix_document_extractions_version_created (document_version_id, created_at DESC)` — supports both "history list" and "latest completed run" lookups with one composite index.

### 4.4 New table: `document_extraction_items`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `extraction_id` | `uuid` FK → `document_extractions(id)` ON DELETE CASCADE, NOT NULL | Items have no existence outside their run. |
| `category` | `text` NOT NULL | `CHECK category IN ('requirement','risk','date','party')`. |
| `item_index` | `integer` NOT NULL | 0-based ordering within `(extraction_id, category)`. |
| `label` | `text` NOT NULL | The extracted item's primary text/value (e.g. a requirement's sentence, a party's name, a date's display value). |
| `detail` | `jsonb` NOT NULL DEFAULT `'{}'` | Category-specific free-form extras (e.g. a date's ISO value, a party's role, a risk's severity) — deliberately schema-light JSONB, mirroring `document_chunks.metadata`'s stated rationale ("semi-structured needs... without schema-migration overhead"), since the roadmap does not mandate a rigid inner schema for these fields. |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE RESTRICT, NOT NULL | Citation-shaped provenance (mirrors `citations`/`conflict_statements` exactly). |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE RESTRICT, NOT NULL | |
| `chunk_id` | `uuid` FK → `document_chunks(id)` ON DELETE RESTRICT, NOT NULL | RESTRICT — an extracted item's evidence cannot be silently deleted out from under it, exactly like `citations.chunk_id`. |
| `page_id` | `uuid` FK → `document_pages(id)` ON DELETE RESTRICT, NOT NULL | |
| `page_number` | `integer` NOT NULL | `CHECK page_number >= 1`. |
| `section` | `text` NULL | |
| `quoted_text` | `text` NOT NULL | Real chunk text (never model-generated) — same `find_quoted_span` mechanism as chat citations. |
| `char_start` | `integer` NULL | `CHECK char_start IS NULL OR char_start >= 0`. |
| `char_end` | `integer` NULL | `CHECK char_end IS NULL OR char_end >= 0`. |
| `relevance_score` | `numeric(6,5)` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT `now()` | |

**Constraints:** `CHECK category IN ('requirement','risk','date','party')` (`ck_document_extraction_items_category`); `UNIQUE (extraction_id, category, item_index)` (`uq_document_extraction_items_run_category_index`).

**Indexes:** `ix_document_extraction_items_extraction_category (extraction_id, category)`; `ix_document_extraction_items_chunk_id (chunk_id)`.

### 4.5 `processing_jobs` amendments

- **Widen the job-type CHECK constraint** to add `'STRUCTURED_EXTRACTION'` (drop `ck_processing_jobs_job_type`, recreate with the new value appended to the existing list — `'EXTRACTION','OCR','CHUNKING','EMBEDDING','INDEXING','COMPARISON','SUMMARY','CONFLICT_SCAN','PURGE','STRUCTURED_EXTRACTION'`).
- **Add** `summary_id uuid NULL FK → document_summaries(id) ON DELETE CASCADE` — mirrors `comparison_id` exactly.
- **Add** `extraction_id uuid NULL FK → document_extractions(id) ON DELETE CASCADE`.
- **Add** `CHECK (job_type = 'SUMMARY') = (summary_id IS NOT NULL)` (`ck_processing_jobs_summary_pairing`) — mirrors `ck_processing_jobs_comparison_pairing`.
- **Add** `CHECK (job_type = 'STRUCTURED_EXTRACTION') = (extraction_id IS NOT NULL)` (`ck_processing_jobs_extraction_pairing`).
- **Add indexes** `ix_processing_jobs_summary_id (summary_id)`, `ix_processing_jobs_extraction_id (extraction_id)`.
- `document_version_id` stays NOT-NULL-except-`CONFLICT_SCAN` (existing `ck_processing_jobs_version_required` constraint is untouched) — both new job types always carry the exact version being summarized/extracted directly in `document_version_id`, so no constraint change is needed there.

### 4.6 Staleness invalidation hook (small, targeted)

Per §2.6 point 4: the **only** existing code path that re-runs `CHUNKING`/`EMBEDDING` for a `document_version_id` that might already have a summary is `JobService.retry_failed_stage` (FAILED → PROCESSING, explicit user action). Add one small, defensive step at the point that stage's chunking/embedding handler successfully re-persists chunks for a version: **if a `document_summaries` row exists for that `document_version_id`, set `stale = true`** (a single `UPDATE ... WHERE document_version_id = :id`, no-op if no row exists). This is the entire "staleness" mechanism — no polling, no scheduled job, no new job type.

### 4.7 Domain model / permissions additions (not new tables, but part of DB-adjacent work)

- `app/domain/state_machines.py`: add `JobType.STRUCTURED_EXTRACTION = "STRUCTURED_EXTRACTION"  # Phase 14` and a `get_max_attempts` entry of `3` (mirrors `COMPARISON`/`SUMMARY` — may call LLM providers).
- `app/domain/permissions.py`: add `SUMMARY_REGENERATE = "summary:regenerate"` and `EXTRACTION_CREATE = "extraction:create"` to `PermissionKey`.

### 4.8 Alembic migration sequence

One new migration file: `backend/alembic/versions/014_document_summaries_and_extractions.py`, `down_revision = "013"`.

**`upgrade()` steps, in order:**
1. `op.create_table("document_summaries", ...)` with all columns/constraints from §4.2.
2. `op.create_index(...)` for the two summary indexes.
3. `op.create_table("document_extractions", ...)` from §4.3.
4. `op.create_index(...)` for the two extraction-run indexes.
5. `op.create_table("document_extraction_items", ...)` from §4.4.
6. `op.create_index(...)` for the two item indexes.
7. `op.add_column("processing_jobs", sa.Column("summary_id", ...))`.
8. `op.add_column("processing_jobs", sa.Column("extraction_id", ...))`.
9. `op.drop_constraint("ck_processing_jobs_job_type", "processing_jobs", type_="check")` then `op.create_check_constraint("ck_processing_jobs_job_type", "processing_jobs", "job_type IN (...)")` with `'STRUCTURED_EXTRACTION'` added.
10. `op.create_check_constraint("ck_processing_jobs_summary_pairing", ...)`.
11. `op.create_check_constraint("ck_processing_jobs_extraction_pairing", ...)`.
12. `op.create_index("ix_processing_jobs_summary_id", ...)`, `op.create_index("ix_processing_jobs_extraction_id", ...)`.
13. Seed the two new permissions + grant to `Admin`/`Editor` system roles, in a `_seed_permissions()` helper function **mirroring migration `013`'s `_seed_conflict_permission` exactly** (INSERT ... ON CONFLICT (key) DO NOTHING into `permissions`; resolve the Admin/Editor role ids; INSERT ... ON CONFLICT DO NOTHING into `role_permissions`).

**`downgrade()`:** exact mirror in reverse order (drop indexes → drop constraints → drop added columns → widen-back the job_type CHECK to the pre-Phase-14 list → drop `document_extraction_items` → drop `document_extractions` → drop `document_summaries`). Leave the seeded permission rows in place on downgrade (matches migration `013`'s documented convention: "role_permissions grants are intentionally left in place on downgrade").

No seed/test data beyond the permission seeding is required by this migration; fixture documents for tests are created by test fixtures (§8), not migrations.

---

## 5. Backend Implementation

### 5.1 Layering summary (nothing new here — confirms the existing rule applies)

```
API (summaries.py / extractions.py)
  → Service (SummaryService / ExtractionService) — business rules, orchestration, authorization
    → Domain (summary_rules.py / extraction_rules.py) — pure algorithms, no I/O
    → Repository (DocumentSummaryRepository / DocumentExtractionRepository) — SQL only
      → Infrastructure (llm.py, embeddings.py, queue.py) — unchanged, reused
```

### 5.2 New domain rule modules

**`app/domain/summary_rules.py`** — pure, unit-testable, no I/O:
- `SUMMARY_CONTEXT_TOKEN_BUDGET` constant reference (actual value lives in `core/config.py`, §5.3; this module just consumes it as a parameter — never hardcodes it, matching `comparison_rules.py`'s convention of named, discoverable constants).
- `select_representative_chunks(sections, chunks, chunks_per_section) -> tuple[list[str], SamplingDisclosure]` — the exact section-diverse sampling algorithm (§5.6). Pure function: given already-loaded `DocumentSection` and `DocumentChunk` rows (or lightweight equivalents), returns the ordered list of selected `chunk_id`s plus a disclosure dataclass. No DB, no LLM — fully unit-testable with in-memory fixture rows, which is what the roadmap's "sampling strategy (section coverage assertion)" unit test targets.

**`app/domain/extraction_rules.py`** — pure constants + helpers:
- `CATEGORY_QUERIES: dict[str, str]` — the four synthesized retrieval queries (§5.9), e.g. `{"requirement": "contractual requirements and obligations", "risk": "risks, liabilities, and penalties", "date": "dates, deadlines, and effective periods", "party": "parties, vendors, customers, and responsible roles"}`.
- `EXTRACTION_TOP_K_PER_CATEGORY`, `EXTRACTION_MAX_ITEMS_PER_CATEGORY` — named constants (values from `core/config.py`, referenced here the same way `comparison_rules.py` references its own thresholds).
- `CATEGORIES: tuple[str, ...] = ("requirement", "risk", "date", "party")`.

### 5.3 `core/config.py` additions

New `Settings` fields (all with sane defaults, all overridable, following the exact style of existing `citation_*`/`search_*` fields):
- `summary_context_token_budget: int = 12000` — larger single-shot budget than chat's `context_token_budget` (5000), since a summary reads much more of the document at once.
- `summary_sampling_chunks_per_section: int = 1` — chunks sampled per top-level section when long-document sampling triggers.
- `extraction_context_token_budget: int = 6000`.
- `extraction_top_k_per_category: int = 8`.
- `extraction_max_items_per_category: int = 20` — defensive cap on LLM output size, mirrors `query_analyzer.py`'s `scope_hints[:10]` bounding pattern.

### 5.4 Worker/background-job changes (read §2.3 first — this is the trap)

**`app/workers/jobs.py` changes:**

1. Add two new special-dispatch functions mirroring `_run_comparison_job` exactly in shape (load domain row by `job.summary_id`/`job.extraction_id`, tenancy check against `job.organization_id`, transition row to `PROCESSING`, call the thin handler, catch `DeterministicJobError`/`Exception` with the identical retry/backoff/dead-letter logic already written for comparisons, mark `COMPLETED`/`FAILED` **on the domain row**, never touch `document_versions.status`):
   - `_run_summary_job(ctx, job, session) -> str`
   - `_run_extraction_job(ctx, job, session) -> str`
2. In `run_processing_job`, **before** the "load version/document" block (i.e., in the same place as the existing `COMPARISON`/`CONFLICT_SCAN` checks around line 1110–1116), add:
   ```
   if JobType(job.job_type) is JobType.SUMMARY:
       return await _run_summary_job(ctx, job, session)
   if JobType(job.job_type) is JobType.STRUCTURED_EXTRACTION:
       return await _run_extraction_job(ctx, job, session)
   ```
3. **Do not** add `JobType.SUMMARY`/`JobType.STRUCTURED_EXTRACTION` to the `HANDLERS` dict. That dict is exclusively for per-version pipeline stages whose completion advances `document_versions.status`; adding these two there would hit the READY-short-circuit bug described in §2.3 and silently no-op every summary/extraction job.
4. `_run_summary_job`/`_run_extraction_job` are **thin** — they call straight into `SummaryService.run(job, summary, session)` / `ExtractionService.run(job, extraction, session)`, which own the actual pipeline logic. This mirrors `ConflictService.run_scan`'s convention (service owns the algorithm; the worker function is orchestration-only), which this plan adopts deliberately over the older `handle_comparison`-owns-the-pipeline-inline convention, because it keeps the pipeline logic reachable and testable from integration tests without going through the Arq/job apparatus (§8.3).

**`app/services/job_service.py` additions**, mirroring `create_for_comparison`/`create_for_org_scan` exactly:
- `JobService.create_for_summary(db, *, organization_id, document_version_id, summary_id) -> ProcessingJob`
- `JobService.create_for_extraction(db, *, organization_id, document_version_id, extraction_id) -> ProcessingJob`

**`app/repositories/processing_job_repository.py`** needs the analogous `create_for_summary`/`create_for_extraction` methods (mirroring the existing `create_for_comparison` method exactly — same INSERT shape, different FK column populated).

### 5.5 New repositories

**`app/repositories/document_summary_repository.py::DocumentSummaryRepository`** (mirrors `DocumentComparisonRepository`'s shape):
- `get_by_version(document_version_id) -> DocumentSummary | None`
- `get_by_id_for_org(summary_id, organization_id) -> DocumentSummary | None`
- `create(*, organization_id, document_id, document_version_id, requested_by) -> DocumentSummary`
- `update_status(summary, status, *, summary_json=None, sampling=None, model=None, prompt_version=None, error_message=None) -> None`
- `mark_stale(document_version_id) -> None` — the §4.6 hook's single write.

**`app/repositories/document_extraction_repository.py::DocumentExtractionRepository`**:
- `create(*, organization_id, document_id, document_version_id, schema_key, requested_by) -> DocumentExtraction`
- `get_by_id_for_org(extraction_id, organization_id) -> DocumentExtraction | None`
- `get_latest_completed_for_version(document_version_id) -> DocumentExtraction | None`
- `list_for_version(document_version_id, *, limit, offset) -> list[DocumentExtraction]`
- `update_status(extraction, status, *, model=None, prompt_version=None, error_message=None) -> None`
- `add_items(extraction_id, rows: Sequence[dict]) -> int` — bulk insert (append-only; not an upsert, since re-runs are new rows by design), mirroring `DocumentChunkRepository.upsert_chunks`'s bulk-insert style but without the `ON CONFLICT` clause.
- `list_items(extraction_id, *, category=None) -> list[DocumentExtractionItem]`

### 5.6 The section-diverse sampling algorithm (`domain/summary_rules.py::select_representative_chunks`)

Deterministic replacement for the roadmap's under-specified "most-central chunk via `rag/retriever.py`" prose (§2.6 point 7):

1. Compute `total_tokens = sum(chunk.token_count for chunk in chunks)` for the version's full chunk set (already loaded via `DocumentChunkRepository.list_for_version`).
2. If `total_tokens <= summary_context_token_budget` → return **all** chunk ids, `SamplingDisclosure(sampled=False, strategy="full")`. This is the common case for ordinary-length policies/SOPs.
3. Otherwise (long-document path):
   a. Identify top-level sections: `[s for s in sections if s.parent_section_id is None]`, ordered by `sort_order`.
   b. For each top-level section, compute its **descendant closure** by walking the already-loaded section list in memory (a section belongs to top-level section `T` if it *is* `T` or its `parent_section_id` chain reaches `T` — bounded, small per-document tree walk, no recursive SQL needed since all sections for the version are already in memory).
   c. Chunks "belonging" to top-level section `T` = chunks whose `section_id` is in `T`'s descendant closure. Order them by `chunk_index` and pick the **middle** `summary_sampling_chunks_per_section` chunk(s) (median position) as the section's representative sample.
   d. **Fallback** for a top-level section with zero chunks tagged via `section_id` (coarse structure detection, or an unstructured document with no sections at all): use `T.start_page`/`T.end_page` to select chunks whose page (via `chunk.page.page_number`) falls in that range instead, same median-position rule.
   e. If there are **no sections at all** for the version (an entirely unstructured long document — the explicitly-supported "No structure detected" case, DB §15/Backend §20), fall back to picking evenly-spaced chunks across the full `chunk_index` range (e.g., every Nth chunk such that the selected set's total tokens fits the budget) — still disclosed as `strategy="section_diverse"` with `included_section_ids=[]`.
   f. Return the selected chunk ids in **original reading order** (not by section), plus `SamplingDisclosure(sampled=True, strategy="section_diverse", included_section_ids=[...], excluded_section_count=<top-level sections not selected from, if chunks_per_section caused any to be skipped — normally 0 since every top-level section contributes>)`.

This is this plan's own proposal (matching PHASE-13's convention of stating "every threshold/algorithm in this document is this plan's own proposal") — implement it exactly as specified, not re-derived, so the unit test's section-coverage assertion is meaningful and reproducible.

### 5.7 `SummaryService` (`app/services/summary_service.py`)

Static methods, explicit `db: AsyncSession` parameter — the exact `ComparisonService`/`ConflictService` convention.

- **`get_or_create_summary(user, document_version_id, db) -> tuple[DocumentSummary, created: bool]`** — mirrors `ComparisonService.get_or_create_comparison`: authorize via `AuthorizationService.authorize_document_version` (404 on failure, never 403-leaks-existence); require `version.status == "READY"` (`ValidationError` otherwise); reuse an existing row via `DocumentSummaryRepository.get_by_version` if one exists (regardless of status — the caller polls); otherwise create a `PENDING` row + `JobService.create_for_summary` + `enqueue_after_commit`, inside one transaction (row + job atomic, per Backend §50).
- **`regenerate_summary(user, document_version_id, db) -> DocumentSummary`** — permission-gated at the API layer by `summary:regenerate`. Unlike `get_or_create_summary`, this **always** creates a fresh job and transitions the existing row back to `PENDING` (creating the row first if none exists yet — regenerate-when-absent behaves like create). The existing `summary` JSONB content is **not cleared** — it stays visible (dimmed, per FE §6.12) until the new job actually completes and overwrites it.
- **`resolve_summary_version(user, document_id, version_number, db) -> str`** — resolves the API's optional `?version=` query param: `None` → `document.current_version_id` (raise `ValidationError` if the document has no current version yet); explicit `version_number` → look up that specific `document_versions` row for the document (404 if it doesn't exist). Always followed by `authorize_document_version`.
- **`resolve_summary_target_from_chat(conversation, analysis, db) -> str | None`** — mirrors `ComparisonService.resolve_comparison_targets_from_chat`'s shape: resolves to exactly one `document_version_id` when the conversation scope is `current_document` or `selected_documents` with **exactly one** active document (using `analysis.temporal_scope` if present, via `resolve_current_version`); returns `None` in every other case (zero, or more than one, document in scope) — the caller sends a clarifying message, never guesses.
- **`run(job, summary, session) -> None`** — the pipeline, called only from `_run_summary_job`:
  1. Load `document_version_id`'s sections (`DocumentSectionRepository.list_for_version`) and chunks (`DocumentChunkRepository.list_for_version`).
  2. `select_representative_chunks(...)` (§5.6) → chunk id list + `SamplingDisclosure`.
  3. Adapt the selected `DocumentChunk` ORM rows into `rag.retriever.SearchResult`-shaped objects (positional relevance score preserving reading order, e.g. `1.0 - (position * epsilon)`, so `build_context`'s dedup/ordering stays meaningful) — a small, local adapter function, not a new context assembler.
  4. `bundle = build_context(adapted_results, budget_tokens=settings.summary_context_token_budget)` — **reused verbatim**.
  5. Call `rag/summary_builder.py::generate_summary_draft(bundle, document_name, version_label, provider)` (§5.8) → parsed `SummaryDraft` (one constrained-JSON-schema LLM call + one retry on parse failure, mirroring `query_analyzer.analyze_query`'s exact retry shape).
  6. Flatten every citable field (`executive_summary`, each `key_points[]`/`dates[]`/`roles[]`/`requirements[]`/`risks[]` item — **not** `topics[]`, which carries no citations) into one ordered pseudo-answer text, recording each item's `(field, item_index, start_offset, end_offset)`.
  7. `extraction = resolve_citations(flattened_text, bundle)`; `validation = await validate_answer(extraction)` — **reused verbatim, Phase 10's exact machinery**.
  8. If `validation.should_regenerate` → one bounded regeneration (mirrors `AskService._regenerate_once`, reusing `CITATION_EMPHASIS_INSTRUCTION`), then re-validate with `allow_regenerate=False`.
  9. Map `validation.claims`' surviving/stripped status back onto the recorded per-item offsets; any item whose claim ends up `uncited`/`unsupported` is **dropped from its list** (never fabricated, never silently kept). If **every** item in a field is dropped, persist that field as an **explicit empty list/string** (never an omitted key) — this is exactly what drives FE §6.12's "No dates identified in this document" explicit-empty-state, not a missing-key ambiguity.
  10. Persist the final `summary` JSONB (surviving items, each carrying `source_index` + the resolved citation object) + `sampling` + `model` + `prompt_version` via `DocumentSummaryRepository.update_status(..., status="COMPLETED", ...)`, and `stale = false`.
  11. Any unhandled exception → `status="FAILED"`, `error_message` set, re-raised so the worker's existing retry/backoff logic applies unchanged.

### 5.8 `rag/summary_builder.py`

- `SummaryDraft` dataclass: `executive_summary: str`, `key_points: list[str]`, `dates: list[str]`, `roles: list[str]`, `requirements: list[str]`, `risks: list[str]`, `topics: list[str]` — every list item (except `topics`) is raw text ending in one or more `[N]` markers, exactly like a chat sentence.
- `generate_summary_draft(bundle, document_name, version_label, provider) -> SummaryDraft` — one `SUMMARY_SYSTEM_PROMPT` + `SUMMARY_USER_TEMPLATE.format(context=bundle.prompt_text, document_name=..., version_label=...)` call, `temperature=0.0`, constrained JSON output; one retry on parse failure (identical shape to `query_analyzer.parse_analyzer_output`/`AnalyzerParseError`); on a second failure, raise (the job fails cleanly rather than persisting a garbage summary).
- No entailment/citation logic lives here — that stays entirely in `rag/citation_validator.py`, called by `SummaryService.run` after this function returns.

### 5.9 `ExtractionService` (`app/services/extraction_service.py`)

- **`create_run(user, document_version_id, schema_key, db) -> DocumentExtraction`** — requires `extraction:create`; authorize version (404-not-403); require `READY`; **always** creates a new `PENDING` row + `JobService.create_for_extraction` + `enqueue_after_commit` (no reuse — every explicit call is a new audit-trail entry, per §2.6 point 3).
- **`get_or_create_run_from_chat(user, document_version_id, db) -> tuple[DocumentExtraction, created: bool]`** — chat-only path: reuse `DocumentExtractionRepository.get_latest_completed_for_version` if one exists; otherwise behaves like `create_run`. This asymmetry (§2.6 point 5) exists specifically so a casual "what are the requirements in this doc" chat question never triggers a fresh, costly extraction run when a good one already exists.
- **`resolve_extraction_target_from_chat(conversation, analysis, db) -> str | None`** — identical shape/constraints to `SummaryService.resolve_summary_target_from_chat` (single document in scope, else `None`).
- **`get_run(user, extraction_id, db) -> DocumentExtraction`**, **`list_runs_for_document(user, document_id, db, *, limit, offset)`** — `document:read`-gated reads, org-scoped, 404-not-403.
- **`run(job, extraction, session) -> None`** — the pipeline, called only from `_run_extraction_job`:
  1. For each of the four categories in `extraction_rules.CATEGORIES`, embed `extraction_rules.CATEGORY_QUERIES[category]` via the existing `EmbeddingProvider`, then call `DocumentChunkRepository.semantic_search(organization_id=..., version_ids=[document_version_id], query_vector=..., top_k=extraction_top_k_per_category)` — **directly**, the same repository method `ConflictService.generate_candidates_for_chunk` already calls directly, bypassing `HybridRetriever`'s multi-document scope-resolution layer because the target version is already known and already authorized (no ambiguity to resolve).
  2. Union + dedupe the four categories' `ChunkSearchResult` sets (a chunk may legitimately surface as evidence under more than one category); adapt to `SearchResult` (mirrors §5.7 step 3) and `build_context(..., budget_tokens=settings.extraction_context_token_budget)` — **reused verbatim**.
  3. Call `rag/extraction_builder.py::generate_extraction_draft(bundle, provider)` (§5.10) → parsed `ExtractionDraft` (`{"requirement": [...], "risk": [...], "date": [...], "party": [...]}`, each item's text ending in `[N]` marker(s), `extraction_max_items_per_category` truncating any oversized category before persistence).
  4. Flatten → `resolve_citations` → `validate_answer` → map back to `(category, item_index)` — **identical reuse pattern to §5.7 steps 6–9**, applied to four flat lists instead of six.
  5. Bulk-insert surviving items via `DocumentExtractionRepository.add_items` (each carrying its category, label, detail, and full citation-shaped provenance); update the run to `COMPLETED` with `model`/`prompt_version`.
  6. Same `FAILED` handling as `SummaryService.run`.

### 5.10 `rag/extraction_builder.py`

- `ExtractionDraft` dataclass mirroring `SummaryDraft`'s shape but with the four fixed categories.
- `generate_extraction_draft(bundle, provider) -> ExtractionDraft` — one `EXTRACTION_SYSTEM_PROMPT`/`EXTRACTION_USER_TEMPLATE` call, identical retry-on-parse-failure shape.

### 5.11 Chat narration modules

**`rag/summary_narration.py::narrate_summary(summary: DocumentSummary) -> str`** — **no LLM call.** Deterministic Markdown-ish rendering of the already-persisted, already-validated `summary` JSONB (executive summary paragraph, then labeled bullet sections) — the exact same text/citations that would render in the Summary screen, just flattened into chat prose. This is possible (and cheaper/faster than comparison/conflict narration) specifically because summary content is already prose; there is nothing left to "narrate."

**`rag/extraction_narration.py::narrate_extraction(items, provider) -> str`** + **`_fallback_extraction_narration(items) -> str`** — mirrors `conflict_narration.py`'s `narrate_conflicts`/`fallback_conflict_narration` exactly: one LLM call instructed to phrase **only** the provided items (never invent new ones — same "narrate, never originate" prompt discipline), with a deterministic template fallback when no provider is configured.

### 5.12 `ChatService` dispatcher changes (`app/services/chat_service.py`)

Inside the existing `try:` block in `message_stream` (the one that already handles `COMPARISON`/`CHANGE_DETECTION`/`CONFLICT_DETECTION`), add two more `elif` branches in the identical shape:

- **`elif analysis.intent == "SUMMARY":`** → `SummaryService.resolve_summary_target_from_chat(...)`; `None` → persist a clarifying assistant turn ("I can summarize a document for you, but I need exactly one document in scope..."); otherwise `get_or_create_summary(...)`; if not `COMPLETED` → persist the "Generating a summary — ask again shortly" acknowledgement (reusing `_persist_comparison_turn` as-is — it is already generic over `content`/`citations`/an optional linked-resource id, so no rename is required, though renaming it to `_persist_structured_turn` is an optional, purely cosmetic cleanup); if `COMPLETED` → `narrate_summary(summary)` (no LLM), attach the summary's own stored citations re-hydrated into `ResolvedCitation` objects, persist via `_persist_comparison_turn`.
- **`elif analysis.intent == "EXTRACTION":`** → `ExtractionService.resolve_extraction_target_from_chat(...)`; `None` → clarifying turn; otherwise `get_or_create_run_from_chat(...)`; not `COMPLETED` → acknowledgement turn; `COMPLETED` → `narrate_extraction(items, provider)` (LLM call), attach up to 10 items' stored citations (bounded, mirrors the existing `changes[:10]`/statement `[:10]` bounds in the comparison/conflict narration helpers), persist.
- The surrounding `except Exception: logger.exception(...)` fallthrough to standard `AskService.ask_stream` is **unchanged** and is exactly the mechanism that satisfies "misclassification degrades gracefully" for every intent, including these two new ones — no additional code needed for that requirement.

**`rag/query_analyzer.py` change:** `DEFERRED_INTENTS` becomes `frozenset()` (empty). The `is_deferred_intent` property and its associated log line become dead but harmless; removing them is optional cleanup, not required for correctness.

### 5.13 API endpoints, request/response contracts

New router `app/api/summaries.py` (registered alongside `compare.py`/`conflicts.py`):

- **`GET /summaries/{document_id}?version={version_number}`** — `document:read`. Get-or-create-and-poll in one endpoint (matches the roadmap's literal API list): resolves the target version (§5.7), calls `get_or_create_summary`, returns the current row's status/content. `202 Accepted` when a new job was just created; `200 OK` otherwise (mirrors `compare.py`'s status-code convention exactly).
- **`POST /summaries/{document_id}/regenerate`** — `summary:regenerate`. Body: `{"version": int | null}` (defaults to current). Calls `regenerate_summary`; audit-logs `SUMMARY_REGENERATED`. Returns `202 Accepted` + the row (now `PENDING`/`PROCESSING`).

New router `app/api/extractions.py`:

- **`POST /extractions`** — `extraction:create`. Body: `{"document_id": str, "version": int | null, "schema_key": "standard_v1"}`. Calls `create_run`; audit-logs `EXTRACTION_RUN_CREATED`. Returns `202 Accepted` + the new run row.
- **`GET /extractions/{extraction_id}`** — `document:read`. Returns the run's status, and (only when `COMPLETED`) its items grouped by category.
- **`GET /documents/{document_id}/extractions`** — `document:read`. **Addition beyond the roadmap's literal two-endpoint list**, justified explicitly: the roadmap requires extraction results to be "linkable/auditable... like summaries," and a run history is meaningless without a list endpoint to reach it from the Document Workspace. Small, precedent-matching addition (mirrors how comparisons/conflicts already have list endpoints), not scope creep.

**Schemas** (`app/schemas/summary.py`, `app/schemas/extraction.py`) follow `schemas/comparison.py`'s exact `_OrmBase`/`ConfigDict(from_attributes=True)` convention — no new response-shape philosophy.

### 5.14 Error handling

- Analyzer misfire → already handled (§2.1, no new code).
- Schema-constraint violation by the model (malformed JSON) → one constrained retry (§5.8/§5.10), then the job fails cleanly with a logged, specific `error_message` — never a partially-parsed, silently-wrong summary/extraction.
- LLM provider unavailable at generation time → job `FAILED`, retried per the existing `max_attempts=3`/exponential-backoff policy (unchanged worker machinery) — no new retry logic needed.
- LLM provider unavailable at chat-narration time → `narrate_summary` never needs a provider (deterministic); `narrate_extraction`/`comparison`/`conflict` narration all fall back to their deterministic template renderer identically.
- Citation validation strips everything (`validation.emptied` equivalent for a field) → persist the empty field explicitly (§5.7 step 9) — never a fabricated fallback bullet.

### 5.15 Authorization and tenant isolation

Every new service method takes the authenticated `User` and re-derives `organization_id` from it (never from client input); every repository method filters `organization_id = :org_id` as a mandatory predicate; every read re-runs `AuthorizationService.authorize_document_version` (404-not-403), so a later `access_level` change on the document is respected on every subsequent summary/extraction read — identical to `compare.py`'s `_get_authorized_comparison` pattern. No new authorization concept is introduced.

### 5.16 Idempotency and retry behavior

- Summary: `get_or_create_summary` is idempotent on `(document_version_id)` via the table's `UNIQUE` constraint — a concurrent duplicate request hits the constraint and re-fetches the existing row, mirroring `ComparisonService.get_or_create_comparison`'s `IntegrityError` handling exactly.
- Extraction runs are intentionally **not** idempotent at the API level (§2.6 point 3) — each `POST /extractions` call is a new row by design; idempotency at the *chat* entry point is achieved instead by reuse-latest-completed (§5.9), not by a uniqueness constraint.
- Job retries: unchanged machinery (`attempts`/`max_attempts`/`RETRYING`/exponential backoff/dead-letter) — both new job types set `max_attempts=3` and are otherwise ordinary `processing_jobs` rows.

---

## 6. Frontend Implementation

(Assumes §2.5's `frontend/src/lib/api/*` prerequisite is resolved — see that section before starting.)

### 6.1 Routes (`frontend/src/App.tsx`)

Add, inside the existing `/app` `PrivateRoute` block, next to `compare`/`conflicts`:
```
<Route path="documents/:id/summary" element={<SummaryPage />} />
<Route path="documents/:id/extractions" element={<ExtractionListPage />} />
<Route path="documents/:id/extractions/:extractionId" element={<ExtractionDetailPage />} />
```

### 6.2 New feature directory: `frontend/src/features/summary/`

- `SummaryPage.tsx` — the FE §6.12 layout: header (`Summary — {document name} ({version label})`, `[Regenerate]` button gated by whether the current user's role grants `summary:regenerate`, mirroring `ConflictResolutionMenu`'s permission-gating pattern), then `SummarySection` blocks in the exact documented order (Executive Summary, Key Points, two-column Dates/Roles, two-column Requirements/Risks, Topics).
- `SummarySection.tsx` — reusable labeled block component; renders a list of `{text, citation}` items, each with a `CitationBadge` (reused from `features/ask/CitationBadge.tsx` — **do not** build a second citation badge component) that opens the same `SourcePreview`/document-navigation flow already built for chat citations.
- `TopicTagList.tsx` — plain labeled tag list (no citations, per the documented schema).
- `RegenerateButton.tsx` — triggers `useRegenerateSummary()`; shows the "Regenerating…" banner state while the polled summary's `status` is `PENDING`/`PROCESSING` and a prior `summary` payload is still present (dimmed-but-visible, never blanked — FE §6.12 UX rule).

### 6.3 New feature directory: `frontend/src/features/extraction/`

- `ExtractionListPage.tsx` — run history for a document (table: run date, status, "View" link, "Run extraction" button gated by `extraction:create`).
- `ExtractionDetailPage.tsx` — one run's items grouped into four labeled sections (Requirements, Risks, Dates, Parties), each item with a `CitationBadge` — reuses the same `SummarySection`-style rendering, not a parallel component (or literally reuse `SummarySection` if its props generalize cleanly — prefer that over a near-duplicate).

### 6.4 `lib/api/*` additions

- `lib/api/summary.ts` — `getSummary(documentId, version?)`, `regenerateSummary(documentId, version?)`, typed response interfaces matching `schemas/summary.py` exactly (field names in `camelCase` per the existing FE convention — confirm the backend's Pydantic `alias_generator`/`by_alias` convention from `schemas/comparison.py` and match it, do not invent a new casing convention).
- `lib/api/extraction.ts` — `createExtractionRun(documentId, version?)`, `getExtractionRun(extractionId)`, `listExtractionRuns(documentId, {limit, offset})`.

### 6.5 `hooks/queries/` additions

`useSummary.ts` (mirrors `useComparisons.ts` exactly):
- `useSummary(documentId, version?)` — polls while `status` is `PENDING`/`PROCESSING` (same `refetchInterval` + `TERMINAL` set pattern as `useComparison`).
- `useRegenerateSummary()` — mutation; on success, pre-populates the detail query cache with the returned (now-pending) row, exactly like `useInitiateComparison`.

`useExtractions.ts` (mirrors the same conventions):
- `useExtractionRun(extractionId)` — polling.
- `useExtractionRuns(documentId, {limit, offset})`.
- `useCreateExtractionRun()` — mutation.

### 6.6 Document Workspace integration

`DocumentWorkspace.tsx`'s header action bar (`[Ask AI][Compare][Summary]`) already documented in FE §6.5 — add the `Summary` button's `onClick` to navigate to `documents/:id/summary`; add an `Extractions` entry to the `⋯` overflow menu navigating to `documents/:id/extractions` (not a primary header button — extraction is a less-frequent, more specialized action than summary, matching the FE spec's information-density principle of not cluttering the primary action bar).

### 6.7 Analytics (minimal, per §2.6 point 8)

Do **not** build the full FE §6.14 screen in Phase 14. Add one small `Analytics` placeholder replacement: a single `KpiCard` row (Documents, Questions, Grounded Answers %, Citation Coverage %) reading a new minimal backend endpoint `GET /analytics/summary` (`analytics:read`-gated) that computes these four numbers from existing columns (`messages.groundedness`, `citations` count per message, `documents` count) with a simple aggregate query — no charts, no time-range selector, no cost breakdown, no CSV export. This satisfies the roadmap's "begins accumulating... Phase 19 formalizes" framing without building ahead of Phase 15/19's explicit scope.

### 6.8 UX states (per the explicit requirement list)

- **Loading:** skeleton `SummarySection` blocks matching the layout (not one spinner) — FE §6.12 literal requirement.
- **Empty (a section has no items):** render the section with its heading and an explicit "No {dates/roles/requirements/risks} identified in this document" message — driven directly by the persisted empty list (§5.7 step 9), never an omitted key.
- **Error:** "Couldn't generate summary" + Retry; if a prior `summary` payload exists, it stays visible with a warning banner instead of being replaced by the error block.
- **Partial (long-document sampling occurred):** inline disclosure banner reading the persisted `sampling` object, e.g. "Summary based on a representative sample across N sections" (generalizing FE §6.12's "first N pages" copy to the section-diverse strategy, per Backend §43's own note that the disclosure copy generalizes).
- **Regenerating:** existing content stays visible, dimmed, "Regenerating…" banner (never blank).
- **Extraction — no items in a category:** same explicit-empty pattern as summary sections.
- **Citation interaction:** identical hover/click/keyboard behavior to chat citations (§6.7/§12 of the FE doc) — reuse `CitationBadge`/`SourcePreview` verbatim, do not build parallel components.

### 6.9 Reuse of existing design system/components

`CitationBadge`, `SourcePreview`, `CitationList` (from `features/ask/`), `ProcessingStatusBadge`-style status rendering (from `features/documents/`), and `ConflictResolutionMenu`'s permission-gating pattern (from `features/conflicts/`) are all reused as-is. No new citation rendering, no new status-badge vocabulary, no new permission-gating primitive.

---

## 7. End-to-End Data Flows

### 7.1 Summary generation (first view)
```
User opens Document Workspace → clicks "Summary"
  → GET /summaries/{documentId}?version=
    → SummaryService.get_or_create_summary
      → authorize_document_version (404 on failure)
      → no existing row → DocumentSummaryRepository.create (PENDING)
        + JobService.create_for_summary (atomic with the row)
      → enqueue_after_commit
    → 202 Accepted {status: "PENDING"}
  ← FE polls (useSummary refetchInterval)
Arq worker claims job → run_processing_job
  → JobType.SUMMARY → _run_summary_job → SummaryService.run
    → load sections + chunks → select_representative_chunks
    → build_context (reused) → generate_summary_draft (1 LLM call, ≤1 retry)
    → flatten → resolve_citations (reused) → validate_answer (reused)
    → [optional 1 bounded regeneration]
    → map surviving/stripped claims back to fields
    → DocumentSummaryRepository.update_status(COMPLETED, summary_json, sampling, model)
FE poll receives status=COMPLETED → renders sectioned summary with citations
```

### 7.2 Summary regeneration
```
User clicks "Regenerate" (permission: summary:regenerate)
  → POST /summaries/{documentId}/regenerate
    → SummaryService.regenerate_summary → fresh PENDING job, row → PENDING
      (existing `summary` JSONB untouched)
    → AuditLogger.log(SUMMARY_REGENERATED)
  ← 202 Accepted; FE shows dimmed existing content + "Regenerating…" banner
[same worker pipeline as 7.1] → COMPLETED → row's summary JSONB overwritten, stale=false
FE poll receives the new content → banner clears
```

### 7.3 Structured extraction (explicit run)
```
User opens Extractions tab → clicks "Run extraction" (permission: extraction:create)
  → POST /extractions {documentId, version?, schema_key: "standard_v1"}
    → ExtractionService.create_run → new PENDING document_extractions row
      + JobService.create_for_extraction (atomic)
    → AuditLogger.log(EXTRACTION_RUN_CREATED)
  ← 202 Accepted
Arq worker → JobType.STRUCTURED_EXTRACTION → _run_extraction_job → ExtractionService.run
  → 4× (embed category query → DocumentChunkRepository.semantic_search) → union/dedupe
  → build_context (reused) → generate_extraction_draft (1 LLM call, ≤1 retry)
  → flatten → resolve_citations (reused) → validate_answer (reused)
  → map back to (category, item_index) → DocumentExtractionRepository.add_items (bulk insert)
  → run row → COMPLETED
FE poll (GET /extractions/{id}) → renders four category sections, each item cited
```

### 7.4 Query classification → specialized service (chat)
```
User (in an Ask AI conversation) sends "Summarize this policy."
  → ChatService.prepare_message (unchanged: scope validation, USER message persisted first)
  → ChatService.message_stream
    → analyze_query(content) → intent="SUMMARY" (already-existing classifier — no change)
    → SummaryService.resolve_summary_target_from_chat(conversation, analysis, db)
      → exactly one document in scope → version_id
      → get_or_create_summary
        → COMPLETED already? → narrate_summary(summary) [no LLM] → citations attached
        → not yet? → acknowledgement turn ("Generating a summary… ask again shortly")
    → persisted as a normal ASSISTANT Message + Citations (via _persist_comparison_turn,
      the exact same atomic write path every other intent's chat turn already uses)
  ← SSE: start → [tokens, if any] → citation* → done (identical contract to every other intent)
```

### 7.5 Citation resolution/validation (unchanged machinery, now invoked from two more call sites)
```
Any answer text with [N] markers + a ContextBundle
  → resolve_citations(text, bundle)        [rag/citations.py — unchanged]
  → validate_answer(extraction)            [rag/citation_validator.py — unchanged]
  → per-claim: supported | partial | unverified | uncited(stripped) | unsupported(stripped)
  → groundedness: grounded | partial | ungrounded
```
Phase 14's only contribution here is **two new callers** (`SummaryService.run`, `ExtractionService.run`) that flatten their structured drafts into the same shape this pipeline already expects, then map the validated result back into structured fields/items. The validation logic itself is untouched.

---

## 8. Testing Strategy

### 8.1 Unit tests (`backend/tests/unit/`, no I/O, mirrors existing file-per-module convention)

- `test_summary_rules.py` — `select_representative_chunks`: full-document (no sampling) case; long-document case asserting **every** top-level section contributes at least one chunk (the "section coverage assertion" the roadmap names explicitly); zero-section (unstructured document) fallback; section with zero directly-tagged chunks falling back to the page-range method.
- `test_summary_builder.py` / `test_extraction_builder.py` — parse success; malformed-JSON retry-then-raise; oversized category truncation (`extraction_max_items_per_category`).
- `test_query_analyzer.py` (**extend**, do not replace) — assert `DEFERRED_INTENTS` is now empty; assert `SUMMARY`/`EXTRACTION` are still correctly classified from the existing labeled example set (no classifier prompt change needed — this is a regression check, not new classifier work).

### 8.2 Repository/database integration tests (`backend/tests/integration/`, real PostgreSQL+pgvector, AI providers stubbed)

- `test_summary_repository.py` — `UNIQUE(document_version_id)` enforcement; `mark_stale` no-op-when-absent and set-when-present; tenant-isolation matrix entry (two orgs, never cross-org) for every new repository method — this is **mandatory**, not optional, per the existing suite's own convention (`test_repositories.py` already runs this matrix for every prior phase's repositories).
- `test_extraction_repository.py` — multiple runs per version persist as distinct rows; `get_latest_completed_for_version` correctness; `add_items` bulk-insert correctness and category/index uniqueness.
- `test_migrations.py` (**extend**) — migration `014` upgrade/downgrade round-trips cleanly against the existing test DB, following the file's established pattern for prior migrations.

### 8.3 Worker/job tests

- `test_summary_pipeline.py`, `test_extraction_pipeline.py` (integration) — fixture document → job → `SummaryService.run`/`ExtractionService.run` called **directly** (not through the Arq queue — mirrors how `test_comparison_pipeline.py`/`test_conflict_scan_pipeline.py` already call their services directly for speed) → assert persisted, every surviving bullet/item's citation resolves to a real chunk; long-document fixture → assert `sampling.sampled=True` and section-diverse coverage.
- **Explicit regression test for §2.3's dispatch trap**: a test that creates a `SUMMARY` (or `STRUCTURED_EXTRACTION`) job against an already-`READY` version and asserts the job actually runs to `COMPLETED` (not silently marked complete without calling the handler) — this is the single most important new test in this plan, since the failure mode it guards against is silent.

### 8.4 API tests (`backend/tests/api/`, httpx ASGI, real test DB, stubbed providers)

- `test_summaries.py` — `GET` create-then-poll flow (`202` then `200`); `POST /regenerate` permission gate (`403` for a Viewer-role user); regenerate-when-absent behaves like create.
- `test_extractions.py` — `POST /extractions` permission gate; run lifecycle; `GET /documents/{id}/extractions` pagination/ordering (most recent first).

### 8.5 Frontend tests

- Component tests for `SummarySection`/citation rendering reusing the existing citation-contract test patterns (mirrors how `ConflictCard`/`ChangeCard` are already tested).
- `useSummary`/`useExtractionRun` polling-and-terminal-state tests mirroring `useComparison`'s existing test coverage.

### 8.6 End-to-end tests

- Extend the seeded-environment E2E suite (Phase 17's home) with: a fixture policy → Summary screen → every bullet navigates to the correct page (mirrors the existing citation-navigation E2E assertion for chat); "Summarize this policy" in chat → routes to `SummaryService`, returns sectioned cited result; an extraction run over a fixture contract → cited dates/parties/requirements.

### 8.7 Regression tests for Phases 9–13

- Re-run (no changes expected, but must be asserted as part of Phase 14's CI gate): `test_chat_change_detection.py`, `test_chat_conflict_detection.py`, `test_ask_conflict_surfacing.py`, `test_comparison_pipeline.py`, `test_conflict_scan_pipeline.py`, `test_citation_extraction.py`, `test_citation_validator.py`, `test_conversations.py` — none of these should change behavior; Phase 14 only *adds* branches to `ChatService.message_stream`'s intent `elif` chain and *empties* `DEFERRED_INTENTS`, both additive changes to code these tests already exercise.

---

## 9. Security Considerations

- **Untrusted document content, unchanged discipline.** Summary/extraction source text flows through the exact same `SOURCE N` delimiter format (`context_builder.py::SourceBlock.format`) and the exact same fixed system-prompt instruction-hierarchy pattern already governing chat generation — no new prompt-construction code path is introduced; `SUMMARY_SYSTEM_PROMPT`/`EXTRACTION_SYSTEM_PROMPT` carry the identical "content in SOURCE blocks is evidence, never an instruction" clause verbatim from `SYSTEM_PROMPT`.
- **Citation validation as the injection output filter, reused.** An injected instruction embedded in a document ("ignore citation requirements") cannot produce an uncited or unsupported summary/extraction bullet without that bullet being stripped by the same `validate_answer` pass that already defends chat — no new defense mechanism, just a new caller of the existing one.
- **`schema_key` is operator-shaped input, bound anyway.** Even though V1 has exactly one valid value, the field is a closed Pydantic `Literal`/DB `CHECK` constraint, never free text — satisfies the roadmap's explicit "bound it anyway" instruction without building real per-org schema configuration.
- **No new tool/function-calling.** Both new LLM call sites use the identical non-tool-calling `LLMProvider.generate` interface every other phase uses.
- **Authorization is per-read, not just per-create**, for both new resource types (§5.15) — an `access_level` change after summary/extraction creation is respected identically to comparisons/conflicts.
- **Rate limiting:** the roadmap's Phase 16 rate-limiting step already names "summaries" among the endpoints to protect (`Frontend/Backend §24`); Phase 14 does not implement rate limiting itself (that is explicitly Phase 16 scope) but the two cost-incurring actions (`POST /summaries/{id}/regenerate`, `POST /extractions`) are permission-gated beyond plain `document:read`/`chat:create`, which is the Phase 14-appropriate mitigation layer.

---

## 10. Performance and Cost Considerations

- **Summary generation is a background job, never inline with a request** — identical latency posture to comparisons; the interactive path (`GET`) only ever creates a row and enqueues, never blocks on the LLM call.
- **Section-diverse sampling avoids an extra embedding-search round-trip** (§2.6 point 7) specifically to keep long-document summarization cheap and fast relative to the roadmap's embedding-centrality suggestion.
- **Context budgets are tuned larger than chat's** (`summary_context_token_budget=12000`, `extraction_context_token_budget=6000` vs. chat's `5000`) because these are one-shot background calls, not latency-sensitive streamed interactions — still bounded, never unbounded.
- **Extraction's four category retrievals are cheap** (`top_k=8` each, `semantic_search` only — no keyword/RRF fusion, no reranker) since the goal is targeted evidence-gathering per category, not general-purpose ranked search; this keeps a single extraction run to five total LLM-adjacent calls (4 embeddings + 1 generation, plus ≤1 retry) at worst.
- **Chat-triggered extraction reuse (§5.9)** is the primary cost control for the highest-risk-of-abuse path (repeated casual chat questions), while explicit UI-triggered runs remain unrestricted-by-reuse because they are a deliberate user action gated by `extraction:create`.
- **Token/cost attribution:** both new LLM call sites should record `prompt_tokens`/`completion_tokens` on their respective rows (`document_summaries.model`/`document_extractions.model` plus an equivalent token-count pair — add `prompt_tokens`/`completion_tokens integer NULL` columns to both new tables in the same migration, mirroring `messages.prompt_tokens`/`completion_tokens`) so Phase 19's cost-formalization work has data to consume from day one, per the roadmap's "Analytics groundwork... token/cost attribution per summary/extraction run" instruction.

---

## 11. Implementation Order

**Sub-phase A — Database (sequential, must land first):**
1. Migration `014` (§4.8): tables, columns, constraints, indexes, permission seeding.
2. `domain/state_machines.py`, `domain/permissions.py` enum additions.
3. ORM models: `app/models/summary.py` (`DocumentSummary`), `app/models/extraction.py` (`DocumentExtraction`, `DocumentExtractionItem`) — mirror `app/models/comparison.py`'s docstring/structure conventions.

**Sub-phase B — Backend core (mostly parallelizable once A lands):**
4. Repositories (`document_summary_repository.py`, `document_extraction_repository.py`, `processing_job_repository.py` additions) — parallel with 5.
5. Domain rule modules (`summary_rules.py`, `extraction_rules.py`) — parallel with 4; fully unit-testable in isolation before any service exists.
6. `core/config.py` settings additions — trivial, do anytime after A.
7. `rag/prompts.py` additions (`SUMMARY_*`, `EXTRACTION_*` prompt triples) — parallel with 4/5.
8. `rag/summary_builder.py`, `rag/extraction_builder.py` — depend on 7; parallel with each other.
9. `rag/summary_narration.py`, `rag/extraction_narration.py` — depend on 7 (extraction narration needs its own prompt); parallel with 8.

**Sub-phase C — Services + workers (sequential after B):**
10. `SummaryService`, `ExtractionService` — depend on 4, 5, 8.
11. `job_service.py` additions (`create_for_summary`, `create_for_extraction`) — depend on 4.
12. `workers/jobs.py` dispatch changes (§5.4) — depend on 10, 11. **Implement and test §8.3's dispatch-trap regression test immediately after this step, before moving on** — this is the highest-risk silent-failure point in the whole plan.

**Sub-phase D — API + chat routing (after C):**
13. `schemas/summary.py`, `schemas/extraction.py`.
14. `api/summaries.py`, `api/extractions.py`, router registration.
15. `ChatService.message_stream` dispatcher additions (§5.12) + `query_analyzer.py`'s `DEFERRED_INTENTS` emptying — these two are tightly coupled and should land together.

**Sub-phase E — Frontend (parallelizable with C/D once API contracts in §5.13/§13 are frozen; can start against a mocked API):**
16. `lib/api/summary.ts`, `lib/api/extraction.ts`.
17. `hooks/queries/useSummary.ts`, `useExtractions.ts`.
18. `features/summary/*`, `features/extraction/*` components + routes.
19. Document Workspace header/menu wiring (§6.6).
20. Minimal analytics endpoint + KPI row (§6.7) — fully independent, can happen anytime.

**Sub-phase F — Testing consolidation (continuous, but a final gate before merge):**
21. Full test suite per §8, including the §8.3 regression test and the §8.7 Phase 9–13 regression re-run.

**What can run in parallel:** everything inside B; everything inside E once contracts are frozen; §6.7 (analytics) at any point after migration 014. **What must be sequential:** A → B → C → D (each sub-phase's services genuinely depend on the previous sub-phase's tables/repositories/prompts existing); step 12's dispatch wiring must be immediately followed by its regression test, not deferred to sub-phase F.

---

## 12. Files to Create/Modify

### Create

| Path | Purpose |
|---|---|
| `backend/alembic/versions/014_document_summaries_and_extractions.py` | Migration: 3 tables, `processing_jobs` amendments, permission seeding. |
| `backend/app/models/summary.py` | `DocumentSummary` ORM model. |
| `backend/app/models/extraction.py` | `DocumentExtraction`, `DocumentExtractionItem` ORM models. |
| `backend/app/repositories/document_summary_repository.py` | `DocumentSummaryRepository`. |
| `backend/app/repositories/document_extraction_repository.py` | `DocumentExtractionRepository`. |
| `backend/app/domain/summary_rules.py` | `select_representative_chunks`, `SamplingDisclosure`. |
| `backend/app/domain/extraction_rules.py` | Category constants, query strings, bounds. |
| `backend/app/rag/summary_builder.py` | `generate_summary_draft`, `SummaryDraft`. |
| `backend/app/rag/extraction_builder.py` | `generate_extraction_draft`, `ExtractionDraft`. |
| `backend/app/rag/summary_narration.py` | `narrate_summary` (deterministic). |
| `backend/app/rag/extraction_narration.py` | `narrate_extraction`, `_fallback_extraction_narration`. |
| `backend/app/services/summary_service.py` | `SummaryService`. |
| `backend/app/services/extraction_service.py` | `ExtractionService`. |
| `backend/app/schemas/summary.py` | Pydantic request/response models. |
| `backend/app/schemas/extraction.py` | Pydantic request/response models. |
| `backend/app/api/summaries.py` | `GET /summaries/{id}`, `POST /summaries/{id}/regenerate`. |
| `backend/app/api/extractions.py` | `POST /extractions`, `GET /extractions/{id}`, `GET /documents/{id}/extractions`. |
| `backend/app/api/analytics.py` | Minimal `GET /analytics/summary` (§6.7). |
| `backend/tests/unit/test_summary_rules.py`, `test_summary_builder.py`, `test_extraction_builder.py` | Unit coverage. |
| `backend/tests/integration/test_summary_repository.py`, `test_extraction_repository.py`, `test_summary_pipeline.py`, `test_extraction_pipeline.py` | Integration coverage, incl. §8.3's dispatch-trap regression test. |
| `backend/tests/api/test_summaries.py`, `test_extractions.py` | API coverage. |
| `backend/tests/fixtures/summary_fixtures.py`, `extraction_fixtures.py` | Fixture documents/sections/chunks (mirrors `comparison_fixtures.py`/`conflict_fixtures.py`). |
| `frontend/src/lib/api/summary.ts`, `extraction.ts` | Typed API clients. |
| `frontend/src/hooks/queries/useSummary.ts`, `useExtractions.ts` | React Query hooks. |
| `frontend/src/features/summary/SummaryPage.tsx`, `SummarySection.tsx`, `TopicTagList.tsx`, `RegenerateButton.tsx`, `summary.css`, `index.ts` | Summary screen. |
| `frontend/src/features/extraction/ExtractionListPage.tsx`, `ExtractionDetailPage.tsx`, `extraction.css`, `index.ts` | Extraction screens. |

### Modify

| Path | Change |
|---|---|
| `backend/app/domain/state_machines.py` | Add `JobType.STRUCTURED_EXTRACTION`; add its `get_max_attempts` entry. |
| `backend/app/domain/permissions.py` | Add `SUMMARY_REGENERATE`, `EXTRACTION_CREATE`. |
| `backend/app/core/config.py` | Add the five new settings fields (§5.3). |
| `backend/app/rag/prompts.py` | Add `SUMMARY_*`, `EXTRACTION_*`, `EXTRACTION_NARRATION_*` prompt triples. |
| `backend/app/rag/query_analyzer.py` | Empty `DEFERRED_INTENTS`; optionally remove the now-dead `is_deferred_intent` log branch. |
| `backend/app/services/chat_service.py` | Two new `elif` branches in `message_stream`; two new `_narrate_*_outcome` helpers. |
| `backend/app/services/job_service.py` | `create_for_summary`, `create_for_extraction`. |
| `backend/app/repositories/processing_job_repository.py` | `create_for_summary`, `create_for_extraction` (mirrors `create_for_comparison`). |
| `backend/app/workers/jobs.py` | `_run_summary_job`, `_run_extraction_job`; two new early-return dispatch checks in `run_processing_job` (§5.4). |
| `backend/app/services/audit_logger.py` | Add `SUMMARY_REGENERATED`, `EXTRACTION_RUN_CREATED` to `AuditAction`. |
| `backend/app/ingestion/chunking_stage.py` **or** `embedding_stage.py` (whichever owns the re-chunk-on-retry path) | Add the §4.6 staleness invalidation hook (one `UPDATE` call, defensive/no-op-safe). |
| `backend/app/api/__init__.py` (or wherever routers are included — mirror the existing registration point for `compare.py`/`conflicts.py`) | Register `summaries.py`, `extractions.py`, `analytics.py` routers. |
| `frontend/src/App.tsx` | Three new routes (§6.1). |
| `frontend/src/features/documents/DocumentWorkspace.tsx` | Wire the `Summary` header button and `Extractions` overflow-menu entry. |
| `backend/tests/unit/test_query_analyzer.py` | Extend: assert `DEFERRED_INTENTS` is empty; regression-check `SUMMARY`/`EXTRACTION` classification. |
| `backend/tests/integration/test_migrations.py` | Extend: migration `014` up/down round-trip. |
| `backend/tests/integration/test_repositories.py` | Extend: tenant-isolation matrix entries for the two new repositories. |

---

## 13. API Contract Changes

| Method | Path | Auth | New/Changed | Request | Response |
|---|---|---|---|---|---|
| `GET` | `/summaries/{documentId}?version=` | `document:read` | New | query: `version?: int` | `202`/`200` `SummaryResponse {id, status, summary?, sampling?, model?, promptVersion?, stale, errorMessage?, createdAt, completedAt?}` |
| `POST` | `/summaries/{documentId}/regenerate` | `summary:regenerate` | New | `{version?: int}` | `202` `SummaryResponse` (status reset to PENDING) |
| `POST` | `/extractions` | `extraction:create` | New | `{documentId, version?: int, schemaKey: "standard_v1"}` | `202` `ExtractionRunResponse {id, status, schemaKey, model?, errorMessage?, createdAt, completedAt?}` |
| `GET` | `/extractions/{extractionId}` | `document:read` | New | — | `ExtractionRunResponse` + (if COMPLETED) `items: {requirement[], risk[], date[], party[]}` each `{label, detail, citation}` |
| `GET` | `/documents/{documentId}/extractions?limit=&offset=` | `document:read` | New (addition beyond roadmap's literal list, §5.13) | query pagination | `{items: ExtractionRunResponse[], total}` |
| `GET` | `/analytics/summary` | `analytics:read` | New (minimal, §6.7) | — | `{documentCount, questionCount, groundedAnswerPct, citationCoveragePct}` |
| `POST` | `/chat/conversations/{id}/messages` | (existing) | **Unchanged contract** — SSE event sequence identical; `intent` internally may now resolve to `SummaryService`/`ExtractionService` | (unchanged) | (unchanged — same `start/token/citation/conflict_notice/error/done` shape) |

No existing endpoint's request/response shape changes. No breaking changes to any Phase 9–13 contract.

---

## 14. Database Migration Plan

- **File:** `backend/alembic/versions/014_document_summaries_and_extractions.py`, `down_revision = "013"`. This is the **only** migration this phase requires — no data backfill migration is needed since these are brand-new, empty-by-default tables.
- **Order:** exactly as listed in §4.8 (tables before their indexes; `processing_jobs` column additions before the widened CHECK constraint; permission seeding last, mirroring migration `013`'s placement of its own permission seed at the end of `upgrade()`).
- **Rollback:** full `downgrade()` mirror (§4.8); verified by `test_migrations.py`'s existing up/down round-trip harness, extended to cover `014`.
- **Zero-downtime consideration:** all new tables and nullable columns; the only structurally-locking change is the `processing_jobs.job_type` CHECK constraint replacement, which — following migration `012`'s already-established precedent for adding `comparison_id`'s pairing constraint — is a fast metadata-only operation on an unpopulated-for-the-new-values column set (no existing row has `job_type IN ('SUMMARY','STRUCTURED_EXTRACTION')` yet, so constraint validation against existing data is instant).
- **Seed/test data:** none beyond permission seeding; per-test fixture documents (`summary_fixtures.py`, `extraction_fixtures.py`) provide sample sections/chunks for integration tests, following the exact pattern of `comparison_fixtures.py`/`conflict_fixtures.py`/`golden_documents.py`.

---

## 15. Acceptance Criteria / Exit Criteria

Directly from the roadmap's own Phase 14 Exit Criteria, made concrete against this plan's artifacts:

1. **A fixture policy produces a summary whose every bullet navigates to real source text.** → `test_summary_pipeline.py` asserts every persisted item's `chunk_id`/`page_number`/`quoted_text` resolves to real, retrievable chunk content; an E2E test clicks a citation badge on the Summary screen and lands on the correct page.
2. **"Summarize this policy" in chat routes to `SummaryService` and returns the sectioned, cited result.** → `test_chat_summary_routing.py` (new, mirrors `test_chat_change_detection.py`) asserts intent classification → `resolve_summary_target_from_chat` → `get_or_create_summary` → narrated turn with citations, persisted as a normal `ASSISTANT` message.
3. **"Compare 2025 and 2026" still routes to comparison** (regression, unaffected by this phase). → `test_chat_change_detection.py` passes unchanged.
4. **An extraction run over a fixture contract yields cited dates/parties/requirements.** → `test_extraction_pipeline.py` asserts non-empty, citation-backed items in at least the `date`/`party`/`requirement` categories for the fixture contract.
5. **All routed answers share the identical citation/SSE contract.** → `test_chat_summary_routing.py`/`test_chat_extraction_routing.py` assert the SSE event sequence (`start → citation* → done`) matches the existing `test_chat_conflict_detection.py`/`test_chat_change_detection.py` shape byte-for-byte in structure.
6. **Every summary/extraction bullet is citation-validated; invalid/unsupported claims are removed, never fabricated.** → covered by reusing `validate_answer` verbatim (§7.5) plus explicit unit assertions in `test_summary_pipeline.py`/`test_extraction_pipeline.py` for a deliberately-adversarial fixture (a source containing a claim the model might over-generalize).
7. **Sampling is disclosed, never silent, for long documents.** → `test_summary_rules.py`'s section-coverage assertion + `sampling.sampled=True` persisted and rendered in the FE partial-state banner (§6.8).
8. **Regeneration works; staleness is flagged, not silent.** → `test_summaries.py` API test for the regenerate flow; `test_summary_repository.py` for `mark_stale`.
9. **The §8.3 dispatch-trap regression test passes** — a `SUMMARY`/`STRUCTURED_EXTRACTION` job against a `READY` version actually executes its handler and reaches `COMPLETED`, not a silent no-op.
10. **No Phase 9–13 regression** (§8.7's full re-run passes unchanged).

---

## 16. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The §2.3 dispatch trap is implemented naively (handlers added to the `HANDLERS` dict instead of the special-dispatch path) and jobs silently no-op | §5.4's explicit instruction + §8.3's mandatory regression test as a merge gate |
| Summary quality varies across hybrid structure+scan documents | Section-aware, deterministic sampling (§5.6); Phase 18 formal quality tuning is out of this phase's scope by design |
| Intent-classification latency budget — six intents now fully routed, one more `elif` branch per new intent | No new LLM call added to the *classification* stage itself (unchanged `analyze_query`, still ≤5s fast-call budget); the *new* LLM calls (summary/extraction generation, extraction narration) are one-per-turn additions only on the SUMMARY/EXTRACTION paths, never on QUESTION/COMPARISON/CONFLICT_DETECTION paths |
| Scope creep into "arbitrary AI workflows" | §2.6 explicitly closes every open question (single-document scope, fixed schema, no new analytics UI) — implement exactly what's specified, nothing more |
| `frontend/src/lib` gap (§2.5) blocks frontend work entirely | Flagged as a prerequisite check before Sub-phase E begins, not silently worked around |
| Extraction's four category retrievals surface overlapping/redundant chunks across categories | `build_context`'s existing `dedup_sources` (near-duplicate collapse) is reused verbatim on the unioned set — no new dedup logic needed |
| A regeneration or new extraction run is triggered far more often than intended, inflating LLM cost | Permission gates (`summary:regenerate`, `extraction:create`) beyond plain read access; chat-triggered extraction reuse (§5.9); full rate limiting remains Phase 16's explicit responsibility, not duplicated here |

---

## 17. GLM Implementation Checklist

- **Do not start without Task 0**: confirm Phases 0–13's test suite passes in your working copy, and resolve the `frontend/src/lib` question (§2.5/§3) before touching frontend code.
- **Do not re-implement anything Phases 9–13 already build.** If you find yourself writing a second citation-extraction function, a second citation-validation pass, a second context-assembly/dedup routine, a second retrieval-permission-enforcement query, or a second `get_or_create_*`-style idempotent-creation pattern "just for summaries," stop — every one of those is reused by direct import, never duplicated.
- **`JobType.SUMMARY`/`JobType.STRUCTURED_EXTRACTION` go on the special-dispatch path in `run_processing_job` (mirroring `COMPARISON`/`CONFLICT_SCAN`), never in the `HANDLERS` dict.** This is the single most important, least-obvious instruction in this plan (§2.3/§5.4) — a naive implementation compiles, passes a shallow smoke test, and then silently does nothing in production the moment it runs against an already-READY version, which is *every* real invocation.
- **`JobType.EXTRACTION` (Phase 5 ingestion text extraction) and the new `JobType.STRUCTURED_EXTRACTION` (Phase 14 structured-information extraction) are two unrelated things that happen to share a root word.** Do not rename, merge, or reuse either one for the other's purpose.
- **`document_summaries` is one row per version, updated in place. `document_extractions` is one row per run, append-only.** Do not invert this — it is a deliberate, justified asymmetry (§2.6 point 3, §4.1), not an oversight.
- **The LLM never writes to the database and never decides whether a claim is grounded.** Every persistence write happens only after `resolve_citations`/`validate_answer` (the existing Phase 10 machinery) has run over the flattened draft text — exactly the same rule PHASE-13 stated for conflict detection, applied here to summary/extraction claims.
- **Section-diverse sampling is fully specified in §5.6 — implement it exactly as written**, using existing `DocumentSection.start_page/end_page` and `DocumentChunk.section_id/chunk_index` data. Do not add a new embedding-based centrality search; that is a deliberate, documented simplification, not a gap to fill in with your own judgment.
- **Chat-intent routing lives only in `ChatService.message_stream`, never in `AskService.ask_stream`** — this matches the existing precedent for `COMPARISON`/`CHANGE_DETECTION`/`CONFLICT_DETECTION` exactly; do not add SUMMARY/EXTRACTION branching to the standalone `/ask` evaluation-harness endpoint.
- **Authorization is per-read, not just per-create**, for both new resource types — re-run `AuthorizationService.authorize_document_version` on every read, exactly like `compare.py`'s `_get_authorized_comparison`.
- **Do not expand scope.** If you find yourself building organization-configurable extraction schemas, knowledge-base-wide (multi-document) extraction, a full Analytics dashboard with charts/cost breakdown/CSV export, or rate limiting — stop, none of these are Phase 14 (§2.6, §6.7, §9 explicitly exclude them; they belong to later phases named in the roadmap).
- **When in doubt about an existing convention** (file naming, schema naming, response shape, test organization, static-method service style), grep the nearest analogous Phase 12/13 artifact (`ComparisonService`/`document_comparison_repository.py` for `SummaryService`/`document_summary_repository.py`; `ConflictService`/`conflict_repository.py` for `ExtractionService`/`document_extraction_repository.py`) and match it exactly rather than introducing a new pattern.
