# Phase 13 — Conflict Detection
## Implementation Plan

**Audience:** an implementing coding agent (GLM 5.3 Flash) with no prior context on this codebase beyond this document and the source architecture documents. This plan makes every architectural decision explicit. Where the source documents conflict, are silent, or the actual repository disagrees with what a prior plan assumed, this plan states the resolution and marks it **Plan-authored decision** — not a quotation from any source document.

**Prepared by:** direct inspection of the actual repository (backend, database, frontend, tests — as of this writing, Alembic head = revision `011`), cross-referenced against `Documentation/AI-Document-Intelligence-Platform-Implementation-Roadmap.md`, `Documentation/Backend-Architecture-Documentation.md` (§42 Conflict Detection, §40/§41 Comparison/Change Detection, §27 Query Understanding, §13 RBAC, §23 Background Processing, §29/§49/§54), `Documentation/Database-Architecture-Design-Documentation.md` (§25/§26 Comparison/Conflict models, §27 Indexing, §28 Constraints, §29 Soft Delete), `Documentation/Frontend-Design-Documentation.md` (§6.5, §6.10–6.13), and `PHASE-12-IMPLEMENTATION-PLAN.md` in full.

---

## 1. Executive Summary

**The single most important finding of this investigation, stated up front because it changes the shape of this entire plan:** `PHASE-12-IMPLEMENTATION-PLAN.md` was written, but **Phase 12 was never actually implemented in this repository.** Verified directly:

- Alembic head is still `011` (`conversations`) — there is no `012_document_comparisons.py` migration, no `document_comparisons`/`comparison_changes` tables, no `backend/app/models/comparison.py`.
- There is no `comparison_service.py`, no `document_comparison_repository.py`, no `backend/app/api/compare.py`, no `domain/comparison_rules.py`, `domain/section_alignment.py`, or `domain/text_diff.py`.
- `backend/app/domain/versioning.py::resolve_current_version()` **still contains the exact bug** the Phase 12 plan documented and prescribed a fix for (§8.4 of that plan): it looks up a nonexistent `is_current` attribute, which is always `False`, so the no-`as_of` branch always falls back to "latest by `created_at`" and ignores `effective_date`/`expiration_date` entirely.
- `frontend/src/lib/` still does not exist on disk, yet `frontend/src/App.tsx`, `DocumentWorkspace.tsx`, `TocPanel.tsx`, `useDocumentProcessing.ts`, etc. all import from `@/lib/api/*` / `@/lib/query/client`. **The frontend cannot build today**, independent of Phase 13.
- `frontend/src/App.tsx` still renders `PlaceholderPage` for both `/app/compare` routes.
- Zero comparison-related tests exist anywhere in `backend/tests/`.

In short: the repository is in *exactly* the "before Phase 12" state that `PHASE-12-IMPLEMENTATION-PLAN.md`'s own §4 assessment described. Phase 13 cannot "build on the actual mechanisms implemented in Phase 12" because none of those mechanisms exist yet.

**What the repository *does* already contain — deliberate forward-compatible scaffolding for Phase 13 specifically, left in place by earlier phases (mirroring the same pattern the Phase 12 plan found for `JobType.COMPARISON`):**
- `JobType.CONFLICT_SCAN = "CONFLICT_SCAN"` already exists in `app/domain/state_machines.py`, already has a retry-policy entry (`2` attempts), and is already present in `processing_jobs`'s DB `CHECK` constraint (`'COMPARISON','SUMMARY','CONFLICT_SCAN','PURGE'`).
- The query analyzer (`app/rag/query_analyzer.py`) already classifies `CONFLICT_DETECTION` as a `QueryIntent` and lists it in `DEFERRED_INTENTS` — currently logged and silently routed to generic RAG, exactly like `COMPARISON`/`CHANGE_DETECTION` were before Phase 12.
- `app/rag/prompts.py`'s analyzer system prompt already documents `CONFLICT_DETECTION` as "asks whether documents contradict each other."
- The frontend's `processingStatus.ts` already has `JOB_TYPE_LABELS.CONFLICT_SCAN = 'Conflict scan'` pre-seeded, and `App.tsx`'s placeholder `DashboardPage` already renders an "Open Conflicts" KPI stat card (currently showing `—`).
- `app/core/exceptions.py::ConflictError` (a generic 409, unrelated in name only) and the `AuditLogger`/`AuditAction`/`PermissionKey` patterns Phase 13 needs to extend are all mature and stable.

**Recommended resolution (Plan-authored decision — this is the load-bearing decision for the whole document):** rather than duplicating, shrinking, or re-deriving Phase 12's already-complete, already-detailed implementation plan inside this document, **Phase 13 formally depends on `PHASE-12-IMPLEMENTATION-PLAN.md` being executed in full — all of its 14 implementation-order steps — before any task in this document begins.** This is not scope creep into "Phase 14 work" pulled backward; it is the roadmap's own explicitly documented dependency ("Depends On: Phase 12") made concrete and actionable, using a plan that already exists and is already implementation-ready. Reasons this is the correct call, not just the convenient one:
1. Comparison-derived conflict seeding (one of Phase 13's two detection triggers) is literally impossible without `document_comparisons`/`comparison_changes` and `ComparisonService`.
2. "Compare Sources" — required in the Conflict UI (FE §6.13) — reuses the Comparison view (FE §6.10) verbatim; that view doesn't exist.
3. Effective-date awareness (Phase 13's most safety-critical rule) depends on the *fixed* `resolve_current_version()` and the new `classify_version_state()` function Phase 12 introduces. Building conflict detection against the *currently broken* version resolution would silently misclassify scheduled versions as current — the exact bug Phase 12 already diagnosed and fixed on paper.
4. The frontend cannot build at all without `frontend/src/lib` (Phase 12 Task 13) — every Phase 13 frontend task inherits this blocker transitively.
5. Duplicating a parallel, smaller "just what Phase 13 needs" slice of Phase 12 inside this document would create two divergent specifications for the same tables/services — a direct violation of "reuse Phase 12 wherever possible, do not create a parallel comparison implementation."

**This document therefore assumes, everywhere below, that `PHASE-12-IMPLEMENTATION-PLAN.md`'s Definition of Done (§23) and Acceptance Criteria (§24) have already been met** — i.e., Alembic is at revision `012`, `document_comparisons`/`comparison_changes` exist and are populated by a working `ComparisonService`, `resolve_current_version()` is fixed, `classify_version_state()` exists, `frontend/src/lib` exists and the app builds, and `/app/compare` is fully functional. **§4.1 below documents this gap formally in the required Expected/Current/Gap/Impact/Recommended-resolution format** so it is never silently assumed away. If, at the time this plan is handed to an implementing agent, Phase 12 has since been completed, §4.1 becomes historical context and every other section of this plan applies directly and unmodified.

Beyond that prerequisite, Phase 13 is a genuinely small, well-scoped vertical slice: two new tables (`conflicts`, `conflict_statements`), a `ConflictService` (scan / verify / dedupe / seed / resolve), a cron-triggered background scan reusing the existing Arq `cron` mechanism (already used for `reconciliation_sweep`), a bounded extension to `processing_jobs` for org-wide (not single-version) jobs, four API endpoints, one new intent-routing branch, one new RAG-pipeline hook, and a focused frontend slice (`ConflictCard`, a Conflicts view, TOC markers, inline answer notices) — all built on Phase 12's retrieval, citation, authorization, worker, and comparison machinery, never a parallel implementation of any of it.

---

## 2. Phase Objective

Per the roadmap (verbatim):

> "Detect contradictory information across documents: background corpus-wide scanning and comparison-derived seeding, semantic contradiction verification with effective-date awareness, persisted conflicts with N-statement evidence and a review/resolution workflow, plus inline surfacing in answers when retrieved sources disagree."

Exit criterion (roadmap, verbatim): "The seeded 5-vs-7-day conflict is detected with evidence from both documents and correct severity/topic; the superseded pseudo-conflict is recorded but flagged 'likely resolved'; a resolved conflict never reopens on re-scan; resolution is role-gated and audited; the inline answer notice appears when retrieved sources disagree."

---

## 3. Source Documents

- `Documentation/AI-Document-Intelligence-Platform-Implementation-Roadmap.md` — Phase 13 section (lines ~2058–2175); Phase 12 section (~1928–2055, prerequisite scope); Phase 14 section (~2177–2260, boundary — do not implement `CONFLICT_DETECTION`'s sibling intents `SUMMARY`/`EXTRACTION` here).
- `Documentation/Backend-Architecture-Documentation.md` — §42 (Conflict Detection, the primary spec), §40/§41 (Comparison/Change Detection — the machinery being reused), §27 (Query Understanding — intent routing), §13 (Authorization & RBAC), §23 (Background Processing/Arq), §29 (Idempotency numbering is §49 — see below), §49 (Idempotency), §51 (External Service Failures), §54 (Audit Logging), Flow 6 (Detect Conflict, lines ~2089–2110).
- `Documentation/Database-Architecture-Design-Documentation.md` — §25 (Document Comparison Model — reused, not duplicated), §26 (Conflict Detection Model — the primary schema spec), §27 (Indexing Strategy — `conflicts`/`comparison_changes` index lines), §28 (Constraints/Cascades), §29 (Soft Delete Strategy — `conflicts`/`conflict_statements` are **not** in the soft-delete list).
- `Documentation/Frontend-Design-Documentation.md` — §6.5 (Document Workspace — TOC `[•]` markers), §6.10 (Comparison — "Compare Sources" reuse target), §6.13 (Conflict Detection — the primary UI spec), §6.2 (Dashboard — "Open Conflicts" KPI card), Journey 6 (Detect Conflict → Compare Conflicting Sources, lines 1194–1200), §8.1 (folder structure — `components/conflicts/`).
- `PHASE-12-IMPLEMENTATION-PLAN.md` — in full; this document follows its file/task/section conventions and does not restate what it already specifies.
- **Actual repository state** — verified directly by reading code for every claim in §4 and every path in §24.

---

## 4. Current Implementation Assessment

### 4.1 Phase 12 Implementation Assessment

**Expected:** Per the roadmap's dependency table (`| 13 | 12 | 14 | Frontend conflict UI |`) and Backend §42 ("`ConflictService` supports two triggers... 2. Comparison-derived"), Phase 13 assumes a working `document_comparisons`/`comparison_changes` domain, a `ComparisonService`, comparison worker, `/compare` API, and comparison frontend already exist and are demonstrable per `PHASE-12-IMPLEMENTATION-PLAN.md`'s own Definition of Done.

**Current:** None of it exists. Verified: `backend/alembic/versions/` tops out at `011_conversations.py`; no `models/comparison.py`; no `services/comparison_service.py`; no `api/compare.py`; `backend/app/domain/versioning.py::resolve_current_version()` still has the `is_current`-attribute bug (confirmed by reading the file directly — the docstring even still describes the broken "flagged current version" behavior as the primary path); `frontend/src/App.tsx` lines 198–199 still render `<PlaceholderPage title="Document Comparison" .../>` and `<PlaceholderPage title="Comparison Results" .../>`; `frontend/src/lib/` is absent (confirmed via directory search — zero files).

**Gap:** The roadmap and both architecture documents were written assuming linear phase completion. This repository's actual state does not match that assumption. Additionally, since Phase 12 was never executed, its own **Gap 6** ("`frontend/src/lib` does not exist... `UNKNOWN / REQUIRES DECISION`... confirm with the team whether a canonical copy exists elsewhere") is *also* still an open, unresolved question — it was never answered, only documented.

**Impact on Phase 13:** Every backend task in this plan that says "reuse `ComparisonService`," "reuse `resolve_current_version`," "extend `handle_comparison`," or "reuse the `/compare` API" is reusing code that does not yet exist. Every frontend task that says "reuse the Comparison view for Compare Sources" is reusing a page that is currently a placeholder. Attempting to implement Phase 13 against the current repository as-is would force exactly the outcome this whole exercise is designed to prevent: a parallel, duplicated comparison/versioning implementation built inside "Phase 13," diverging from the canonical (already-written, already-detailed) Phase 12 design.

**Recommended resolution:** Execute `PHASE-12-IMPLEMENTATION-PLAN.md` in full (all of its §22 Implementation Order, Steps 1–14) as a hard prerequisite, **before** starting §23 Task 1 of this document. Do not re-derive, shrink, or reinterpret that plan here — it is already correct and already detailed at the same level of rigor this document aims for. The one exception: this document's §23 "Task 0" below explicitly names the *specific* Phase 12 artifacts Phase 13 code touches or extends, so an implementing agent can verify Phase 12 is actually done (not just "mostly done") before proceeding. If Phase 12's Gap 6 (`frontend/src/lib` provenance) is still unresolved when Phase 13 frontend work begins, Phase 12's own fallback (reconstruct from call-site inference) applies — it is not re-litigated here.

### 4.2 Already Implemented (reuse as-is, once Phase 12 lands)

| Item | Location | Notes |
|---|---|---|
| `JobType.CONFLICT_SCAN` | `app/domain/state_machines.py:45` | Already in the enum, already has a retry-policy entry (`_JOB_RETRY_POLICY[JobType.CONFLICT_SCAN] = 2`), already in `processing_jobs.job_type`'s DB `CHECK` constraint (`app/models/processing_job.py:56`). No enum or CHECK-constraint change needed. |
| Query intent classification | `app/rag/query_analyzer.py:50-67` | `CONFLICT_DETECTION` is already a valid `QueryIntent` literal and already in `DEFERRED_INTENTS`. `analyze_query()` already classifies it; currently logs and falls through to standard RAG (`query_analyzer.py:231-239`, same code path as `COMPARISON` before Phase 12). Needs routing, not classification. |
| `ANALYZER_SYSTEM_PROMPT` | `app/rag/prompts.py:58,69` | Already documents `CONFLICT_DETECTION` in its intent list and description. No prompt change needed for classification (only for the new narration/verification prompts, §11). |
| Frontend job-type label | `frontend/src/features/documents/processingStatus.ts:44` | `JOB_TYPE_LABELS.CONFLICT_SCAN = 'Conflict scan'` already present. Reuse directly for the scan-status/processing UI — no change needed here. |
| Frontend Dashboard KPI slot | `frontend/src/App.tsx:64` (placeholder `DashboardPage`) | An "Open Conflicts" KPI card already exists in the placeholder Dashboard (value `—`, icon `⚠`). Wiring it to real data is in scope (§21); building the rest of the Dashboard is not (Phase 3/10 territory per its own placeholder labeling — leave everything else in that component alone). |
| `AuditLogger` / `AuditAction` | `app/services/audit_logger.py` | Single write-path `AuditLogger.log(db, organization_id, user_id, action, resource_type, resource_id, metadata, request)`; `AuditAction` is a plain string-constant class. Add `CONFLICT_RESOLVED` here — do not build a parallel audit mechanism. |
| `PermissionKey` / RBAC | `app/domain/permissions.py`, migration `002` | `StrEnum` of permission keys seeded via raw SQL `INSERT ... ON CONFLICT DO NOTHING` in `002_identity_tenancy.py::_seed_permissions/_seed_system_roles`. `COMPARISON_CREATE = "comparison:create"` is the direct precedent to mirror for a new `CONFLICT_RESOLVE` key. System roles are actually named **Admin/Editor/Viewer** in code (not the Backend doc's illustrative "Admin, Manager, Employee, Viewer") — use the real names. |
| `Citation` model | `app/models/message.py:153-255` | The exact provenance shape (`document_id`/`document_version_id`/`chunk_id`/`page_id` all `NOT NULL ondelete="RESTRICT"`, plus denormalized `page_number`/`section`/`quoted_text`) `conflict_statements` should mirror, per DB §26's explicit statement that conflicts reuse "the same citation-grade precision as the citations table." |
| `DocumentChunkRepository.semantic_search()` | `app/repositories/document_chunk_repository.py:333-454` | Takes a raw `query_vector: list[float]` directly (not text) — this is exactly what candidate generation needs: pass a chunk's own stored `embedding` as the query vector, no re-embedding. Needs one small additive extension (§10). |
| `arq.cron` scheduling | `app/workers/main.py:77-119` | `WorkerSettings.cron_jobs` already uses `arq.cron(...)` for `reconciliation_sweep`. Direct precedent for a new cron entry — no new scheduler needed. |
| `TenantScopedRepository` | `app/repositories/base.py` | Structural org-scoping base class every tenant-owned repository extends. `ConflictRepository` extends this directly. |
| `LLMProvider`/`EmbeddingProvider` abstractions | `app/infrastructure/llm.py`, `app/infrastructure/embeddings.py` | `generate()`/`embed()`, `StubLLMProvider`/`StubEmbeddingProvider` for tests. Reuse directly for the contradiction-check call and narration call. |
| `organizations.settings` JSONB critical-sections key | Introduced by Phase 12 (`organization.settings["comparison"]["critical_sections"]`) | Reuse the *same* key and lookup helper for conflict severity's "is this a critical topic" signal (§12) — do not invent a second, conflict-specific settings key. |

### 4.3 Partially Implemented (exists but blocks or is inconsistent with Phase 13, independent of the Phase 12 gap)

1. **`app/domain/versioning.py::resolve_current_version()`** — broken exactly as Phase 12's plan §8.4 describes; must be fixed as part of executing Phase 12 (§4.1), not re-fixed here. Restated because Phase 13's effective-date awareness (§12) has zero margin for this bug: a broken "current version" resolution would make Phase 13 flag scheduled-but-not-yet-effective versions as active conflicts.
2. **`app/rag/hybrid_search.py::HybridRetriever.search()`** — the retrieval path `AskService` actually calls (not `SemanticRetriever` directly, though `SemanticRetriever` is what `ChunkRepository.semantic_search` sits under). §20's inline-surfacing hook attaches after this call returns, not inside it — no modification to `HybridRetriever` itself is needed, only a new step in `AskService.ask_stream` after retrieval.
3. **`app/services/ask_service.py:268`** — calls `analyze_query()` but never branches on `analysis.intent`; every intent (including the now-classified-but-unrouted `CONFLICT_DETECTION`) proceeds through generic RAG. Phase 12 adds the `COMPARISON`/`CHANGE_DETECTION` branch here; Phase 13 adds a sibling `CONFLICT_DETECTION` branch in the same location (§19).

### 4.4 Missing (build from scratch, Phase 13's actual scope)

- `conflicts` and `conflict_statements` tables/models/migration — confirmed absent (no migration, no model file, zero hits searching for `conflicts`/`conflict_statements` as table names anywhere in `backend/`).
- `ConflictService`, `ConflictRepository`, the conflict background-scan worker handler, candidate generation, the contradiction-check LLM call + parser, `domain/conflict_rules.py` (severity), the dedup/merge algorithm, the effective-date-awareness classification, comparison-derived seeding hook, the resolution state machine — none exist.
- `GET /conflicts`, `GET /conflicts/{id}`, `POST /conflicts/{id}/resolve`, `GET /conflicts/scan-status` endpoints and their Pydantic schemas — none exist.
- `PermissionKey.CONFLICT_RESOLVE` and its role seeding — does not exist.
- `AuditAction.CONFLICT_RESOLVED` — does not exist.
- `CONFLICT_DETECTION` chat-intent routing — the analyzer classifies it; nothing downstream branches on it.
- Inline conflict surfacing in `AskService`/`ChatService` — no hook exists between retrieval and generation for this today.
- All conflict-related tests (unit/integration/API/worker) — zero exist.
- Frontend: `ConflictCard`, `ConflictResolutionMenu`, a Conflicts view, TOC `[•]` markers, the inline answer conflict notice, the Dashboard "Open Conflicts" wiring — none exist beyond the two placeholder scaffolding hooks noted in §4.2.

### 4.5 Required Modifications (existing — post-Phase-12 — code that must change)

1. `app/models/processing_job.py` / a new migration — `document_version_id` must become nullable (org-wide scan jobs have no single version to anchor to); add a nullable `checkpoint JSONB` column for scan-resume state; add a `CHECK` constraint requiring `document_version_id` for every job type *except* `CONFLICT_SCAN`. See §15, §23 Task 2.
2. `app/repositories/document_chunk_repository.py::semantic_search()` — add an optional `exclude_document_id: str | None = None` parameter (one additional SQL predicate) so candidate generation can query "similar chunks in a **different** document." See §10, §23 Task 5.
3. `app/domain/comparison_rules.py` (created by Phase 12) — extract its inline `is_critical_section` lookup into a standalone, reusable function so `ConflictService` can call the *same* function rather than reimplementing the `organizations.settings["comparison"]["critical_sections"]` lookup. See §12, §23 Task 4.
4. `app/services/ask_service.py` — add a `CONFLICT_DETECTION` branch alongside Phase 12's `COMPARISON`/`CHANGE_DETECTION` branch (§19); add the inline-surfacing hook after retrieval, before generation, for **every** intent (§20).
5. Wherever Phase 12 places `handle_comparison`'s completion logic (`app/workers/jobs.py` per that plan's §12 Task 8) — add one call to `ConflictService.seed_from_comparison(comparison_id, db)` immediately after `document_comparisons.status` is set to `COMPLETED`. This is the single, minimal, additive touch-point into Phase-12-owned code (§13).
6. `frontend/src/features/documents/TocPanel.tsx` — accept an optional `conflictSectionIds: Set<string>` prop and render the `[•]` marker (FE §6.5) next to matching nodes. See §21, §23 Task 15.
7. `frontend/src/App.tsx` — replace the placeholder "Open Conflicts" KPI value with real data; no route changes needed (Conflicts view is a *new* route, not a placeholder replacement — see §21).
8. `backend/alembic/env.py` — add `Conflict`, `ConflictStatement` to the explicit model-import list (same reason Phase 12 adds `DocumentComparison`/`ComparisonChange` there).
9. `backend/tests/conftest.py::_CLEANUP_TABLES` — add `"conflict_statements"` and `"conflicts"` in FK-safe order.

### 4.6 Architecture Gaps (explicit, not silently resolved)

**Gap 1 — `detection_method` casing disagrees with every other enum/CHECK-constrained column in this codebase.**
Expected: both the roadmap ("`detection_method background_scan/comparison_derived/retrieval_time`") and Backend §42 use lowercase-snake-case values.
Current: every other CHECK-constrained text column actually in the codebase — `document_versions.status` (`READY`, `FAILED`...), `processing_jobs.status`/`job_type` (`PENDING`, `COMPARISON`...), and (once Phase 12 lands) `comparison_changes.change_type`/`severity` (`ADDED`, `MAJOR`...) — uses UPPER_SNAKE_CASE. The codebase's own forward-compatible scaffolding, `JobType.CONFLICT_SCAN`, is itself uppercase.
Gap: the two architecture documents disagree with the actual codebase convention (they agree with each other, but not with the code).
**Recommended resolution:** use **`BACKGROUND_SCAN` / `COMPARISON_DERIVED` / `RETRIEVAL_TIME`** (uppercase), for internal consistency with every other enum-like column in this codebase — the same reasoning Phase 12's plan applied when it picked `document_comparisons`/`comparison_changes` naming over ambiguous alternatives. This plan uses these values throughout.

**Gap 2 — `processing_jobs` has no natural shape for an org-wide (not single-version) job.**
Expected: Backend "Infrastructure Work" says the scan is "a scheduled (cron) job on the worker; scan checkpointing in job metadata for resumability" — implying reuse of `processing_jobs`. Roadmap: "reusing `rag/retriever.py`/pgvector" and existing worker infra generally.
Current: `processing_jobs.document_version_id` is `NOT NULL` (verified `app/models/processing_job.py:88-92`) — every existing job type is anchored to exactly one document version. A conflict scan is anchored to an **organization**, not a version.
**Recommended resolution:** make `document_version_id` nullable, add a `CHECK` constraint requiring it for every job type except `CONFLICT_SCAN`, and add a nullable `checkpoint JSONB` column (document-level resume cursor + running counters) — the same "small, purpose-specific, nullable column" pattern Phase 12's plan used for `processing_jobs.comparison_id` (its own Gap 3). This is a strict generalization, not a redesign: every other job type's behavior is completely unaffected (their `document_version_id` stays required via the `CHECK`). Full detail in §15, §23 Task 2.

**Gap 3 — The task's illustrative endpoint list includes `GET /conflicts/{id}/statements`; the roadmap's actual endpoint list does not.**
Expected (roadmap, §Phase 13 "APIs", verbatim): `GET /conflicts?status=open` · `GET /conflicts/{id}` · `POST /conflicts/{id}/resolve` · `GET /conflicts/scan-status`. No separate statements endpoint.
Gap: a separate `/statements` endpoint would be a plausible design (Phase 12 has an analogous `GET /compare/{id}/changes` as its own endpoint), but the roadmap is explicit and does not include one, and a conflict's statement count is small (typically 2–4, never paginated at the scale `comparison_changes` can reach).
**Recommended resolution:** embed `statements: list[ConflictStatementItem]` directly in the `GET /conflicts/{id}` response body. No separate endpoint. This plan follows the roadmap's literal, authoritative endpoint list.

**Gap 4 — Dedup/merge identity for an N-statement conflict is never specified by any source document.**
Expected: DB §26 states conflicts model N statements "because more than two documents can disagree on the same point," and requires "Scans never create duplicate open conflicts for the same statement pair" (Backend §42 Business Rules) — but no document specifies the actual matching algorithm that decides when a *new* candidate pair should become a *new* conflict versus grow an *existing* one.
**Recommended resolution:** a fully deterministic, chunk-identity-based algorithm, detailed in §14. No document contradicts this; it is a genuine, previously-unspecified gap this plan closes.

**Gap 5 — `conflict_statements.effective_date` is documented as "denormalized from the version," but effective-date *state* (current/superseded/scheduled) is inherently time-varying.**
Expected: DB §26 stores `effective_date` as a denormalized column "for quick chronological sort" — a static snapshot.
Gap: whether a version is CURRENT/SUPERSEDED/SCHEDULED can change after a conflict is recorded (e.g., a third version is published later, superseding one side). A statically stored classification would go stale silently.
**Recommended resolution:** store `effective_date` as a static denormalized column exactly as documented (needed for display/sort even if the source chunk/version is later hard-deleted, mirroring `comparison_changes.old_text`/`new_text`'s reasoning) — but compute the **CURRENT/SUPERSEDED/SCHEDULED classification live**, at every read, via Phase 12's `classify_version_state()`, never persisted. Detailed in §12.

---

## 5. Phase 13 Scope

### 5.1 In Scope (the M6-adjacent vertical slice this plan targets)

- **Prerequisite:** Phase 12 fully executed per `PHASE-12-IMPLEMENTATION-PLAN.md` (§4.1) — verification checklist in §23 Task 0.
- `conflicts` + `conflict_statements` migration, models, indexes, constraints.
- `PermissionKey.CONFLICT_RESOLVE` + role seeding; `AuditAction.CONFLICT_RESOLVED`.
- `processing_jobs` extension (nullable `document_version_id`, `checkpoint` JSONB) for org-wide jobs.
- `ConflictRepository`, `ConflictService` (candidate generation, contradiction verification, effective-date awareness, dedup/merge, persistence, resolution).
- `domain/conflict_rules.py` (severity), shared `is_critical_section` extraction from `domain/comparison_rules.py`.
- `semantic_search()` extension (`exclude_document_id`).
- Background scan worker handler + Arq cron registration.
- Comparison-derived seeding hook (one call into Phase-12-owned code).
- `GET /conflicts`, `GET /conflicts/{id}`, `POST /conflicts/{id}/resolve`, `GET /conflicts/scan-status`.
- `CONFLICT_DETECTION` chat-intent routing (deterministic query → persisted conflicts, narrated).
- Inline conflict surfacing in `AskService.ask_stream` (deterministic detection among retrieved chunks; SSE `conflict_notice` event).
- Frontend: `ConflictCard`, `ConflictResolutionMenu`, a Conflicts view (`/app/conflicts`, `/app/conflicts/:id`), TOC `[•]` markers, Dashboard "Open Conflicts" wiring, inline chat conflict notice, "Compare Sources" → pre-anchored comparison navigation.
- Full test suite per §27.

### 5.2 Explicitly Out of Scope (do not build in Phase 13)

- Anything belonging to Phase 12 itself — this document does not restate or re-implement `PHASE-12-IMPLEMENTATION-PLAN.md`; it assumes that plan's own Definition of Done.
- `SummaryService`, `EXTRACTION` workflow, and routing for `SUMMARY`/`EXTRACTION` intents — Phase 14.
- Any dismissal-ratio/threshold-tuning analytics dashboard (roadmap "false-positive tuning loop," step 6) — this is an ongoing operational/Phase-19 concern, not a Phase 13 deliverable; Phase 13 only needs the raw data (`status` distribution) to already exist, which it does by construction.
- Per-org configurable scan windows/scheduling UI — V1 uses one fixed nightly cron cadence (§15).
- Merging two already-distinct existing conflicts into one when a later scan finds they're transitively related (§14's documented V1 limitation) — flag and skip, do not implement conflict-graph merging.
- Any new infrastructure component (message bus, dedicated vector DB, new queue system, GraphQL, Kubernetes, Elasticsearch, a second LLM-provider abstraction, a second citation system). Conflict detection is a service + worker inside the existing modular monolith, using the existing Postgres/pgvector/Redis/Arq stack.
- PDF bounding-box highlighting — not a Phase 13 requirement (same exclusion as Phase 12).
- The full Documents list page, full Dashboard, full Analytics page — build only the specific widgets/wiring this plan calls for.

---

## 6. Dependencies

**Depends on:** Phase 12 in full (§4.1) — `document_comparisons`/`comparison_changes`, `ComparisonService`, the fixed `resolve_current_version()`, `classify_version_state()`, the `/compare` API and frontend, `frontend/src/lib`. Also depends on Phase 9–11 (retrieval, chat, RAG — already implemented, verified present: `rag/hybrid_search.py`, `services/ask_service.py`, `services/chat_service.py`).

**Blocks:** Phase 14 (`CONFLICT_DETECTION` intent's dedicated service must exist before Phase 14 finalizes six-intent routing — this plan delivers that service), Phase 15 (conflict UI consolidation).

**Parallelizable within this plan:** the frontend `ConflictCard`/Conflicts-view UI (Task 16) can be built against a mocked API response in parallel with the backend `ConflictService` pipeline (Tasks 5–9), since the API contract (§18) is fixed early. The candidate-generation/verification pipeline (Tasks 5–7) and the comparison-derived-seeding hook (Task 8) can be built in parallel — they share the dedup/persistence layer (Task 6) but are otherwise independent data sources into it.

---

## 7. Target Architecture

```
                    Document Versions (Phase 3/12)
                              │
                    ┌─────────▼──────────┐
                    │  ComparisonService  │  (Phase 12 — reused, not duplicated)
                    └─────────┬──────────┘
                              │ comparison_changes (MODIFIED, MAJOR severity,
                              │ both sides CURRENT)
                              ▼
                    ┌─────────────────────┐        ┌──────────────────────────┐
                    │ Comparison-derived   │        │  Background Scan          │
                    │ seed hook            │        │  (arq cron, nightly)      │
                    │ (one call, §13)      │        │  candidate generation     │
                    └─────────┬───────────┘        │  (pgvector reuse, §10)    │
                              │                     │  contradiction check (LLM)│
                              │                     │  (§11)                   │
                              │                     └─────────┬────────────────┘
                              │                                │
                              ▼                                ▼
                    ┌───────────────────────────────────────────────┐
                    │              ConflictService                    │
                    │  effective-date awareness (§12, reuses          │
                    │  classify_version_state from Phase 12)          │
                    │  dedup / merge (§14)                            │
                    └───────────┬─────────────────────┬───────────────┘
                                │                       │
                    ┌───────────▼──────────┐  ┌─────────▼─────────────┐
                    │ conflicts +           │  │  Resolution Workflow   │
                    │ conflict_statements   │  │  (OPEN→REVIEWED/       │
                    │ (persisted, §9)       │  │   DISMISSED, audited)  │
                    └───────────┬───────────┘  └────────────────────────┘
                                │
                ┌───────────────┼────────────────────┬───────────────────┐
                ▼                ▼                     ▼                   ▼
        GET /conflicts   CONFLICT_DETECTION    Inline surfacing in    Conflict UI
        (list/detail/     chat intent (§19)     Ask AI answers (§20)  (ConflictCard,
        resolve, §18)                                                  TOC markers,
                                                                        Compare Sources
                                                                        → Phase 12 UI)
```

---

## 8. Conflict Domain Model

A conflict is **not** "Document A contradicts Document B." It is a `Conflict` aggregate root with a `topic`, a `severity`, a `status`, and **N `ConflictStatement` rows** (`N ≥ 2`), because a global policy, a regional addendum, and an outdated local memo can all specify a different approval window for the same fact.

```
Conflict (topic, severity, status, detection_method)
   │
   ├── ConflictStatement 1  (document_version_id, chunk_id, statement_text, effective_date)
   ├── ConflictStatement 2  (document_version_id, chunk_id, statement_text, effective_date)
   └── ConflictStatement N  (...)
```

Every `ConflictStatement` carries the same provenance chain a `Citation` does — `document_id` (denormalized), `document_version_id`, `chunk_id`, `page_id`, `page_number`, `section` — **all `NOT NULL`, all `ON DELETE RESTRICT`**, per Backend §46 rule 15's non-negotiable requirement ("every statement carries a real `chunk_id`") and DB §26's explicit statement that this reuses "the same citation-grade precision as the citations table." This is **not** a new evidence model; it is the `Citation` shape, applied to a different aggregate root. `statement_text` is a denormalized snapshot of the source chunk's content (same reasoning as `comparison_changes.old_text`/`new_text`: the statement must remain displayable even if the source chunk is later modified or, in a future phase, deleted).

**Effective-date state (`CURRENT`/`SUPERSEDED`/`SCHEDULED`) is never stored on `ConflictStatement`.** It is computed live, per statement, at every read, via Phase 12's `classify_version_state()` (§12) — because a version's state can change after a conflict is recorded (a new version can supersede one side later). The persisted `effective_date` column is a static, denormalized display value only (matching DB §26's literal column), never the source of the current/superseded classification.

---

## 9. Database Design

**One migration:** `backend/alembic/versions/013_conflicts.py` (`down_revision = "012"` — Phase 12's migration; this plan's migration cannot be written or run before that one exists).

### 9.1 `conflicts`

```sql
CREATE TABLE conflicts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    topic               TEXT NOT NULL,
    severity            TEXT NOT NULL CHECK (severity IN ('MAJOR','MODERATE','MINOR')),
    status              TEXT NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN','REVIEWED','DISMISSED')),
    detection_method    TEXT NOT NULL
                        CHECK (detection_method IN ('BACKGROUND_SCAN','COMPARISON_DERIVED','RETRIEVAL_TIME')),
    resolved_by         UUID REFERENCES users(id) ON DELETE RESTRICT,
    resolution_note     TEXT,
    resolved_at         TIMESTAMPTZ,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_conflicts_resolution_fields CHECK (
        (status = 'OPEN' AND resolved_by IS NULL AND resolved_at IS NULL)
        OR (status <> 'OPEN' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
    )
);
CREATE INDEX ix_conflicts_org_status ON conflicts (organization_id, status);
CREATE INDEX ix_conflicts_org_detected ON conflicts (organization_id, detected_at DESC);
```

`detection_method = 'RETRIEVAL_TIME'` (per Backend §42's enum) is reserved for a hypothetical future write path where a retrieval-time-only disagreement gets persisted rather than surfaced transiently — **Phase 13's inline surfacing (§20) never writes this**; it only *reads* existing `BACKGROUND_SCAN`/`COMPARISON_DERIVED` conflicts among retrieved chunks. The value is kept in the `CHECK` constraint for forward-compatibility (documented exactly like Phase 12 kept `JobType.SUMMARY` in its enum without a handler) but this plan writes only the first two values.

The `ck_conflicts_resolution_fields` constraint (a plan-authored addition, mirroring Phase 12's `truncated` column addition — a small, justified schema strengthening beyond the doc-literal column list) makes "resolved without a resolver" or "OPEN with stale resolver data" structurally impossible, rather than merely conventionally avoided.

### 9.2 `conflict_statements`

```sql
CREATE TABLE conflict_statements (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conflict_id           UUID NOT NULL REFERENCES conflicts(id) ON DELETE CASCADE,
    document_id           UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    document_version_id   UUID NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    chunk_id              UUID NOT NULL REFERENCES document_chunks(id) ON DELETE RESTRICT,
    page_id               UUID NOT NULL REFERENCES document_pages(id) ON DELETE RESTRICT,
    page_number           INTEGER NOT NULL,
    section               TEXT,
    statement_text        TEXT NOT NULL,
    effective_date        DATE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_conflict_statements_conflict_chunk UNIQUE (conflict_id, chunk_id)
);
CREATE INDEX ix_conflict_statements_conflict_id ON conflict_statements (conflict_id);
CREATE INDEX ix_conflict_statements_chunk_id ON conflict_statements (chunk_id);
CREATE INDEX ix_conflict_statements_document_version_id ON conflict_statements (document_version_id);
```

**Why `RESTRICT`, not `SET NULL`/`CASCADE`, on the provenance FKs (unlike `comparison_changes.old_chunk_id`/`new_chunk_id`, which are nullable `SET NULL`):** Backend §46 rule 15 makes a real `chunk_id` non-negotiable for every statement — a conflict statement with no evidence is not a valid conflict statement at all, whereas a comparison change can legitimately lose one side's chunk reference and still display its denormalized snapshot. This mirrors `Citation`'s FK policy exactly ("cited evidence cannot be silently deleted out from under a persisted answer") — applied here because no document-version-deletion flow exists in this codebase today (verified, same finding Phase 12's plan made for its own FKs), so `RESTRICT` is the safe default until one exists.

**`uq_conflict_statements_conflict_chunk`** enforces at the database level that the same chunk can never appear twice as a statement within one conflict (defense-in-depth under the application-level dedup/merge logic in §14, which already prevents this from being attempted, but a unique constraint makes the invariant structurally guaranteed rather than merely convention).

**No soft-delete column on either table** — per DB §29, `conflicts`/`conflict_statements` are not in the soft-delete list (same category as `messages`/`citations`/`audit_logs`: immutable historical record). A `DISMISSED` conflict is never deleted, only status-transitioned — this is the entire point of persisting conflicts instead of regenerating them (DB §26).

### 9.3 `processing_jobs` extension (Gap 2, §4.6)

```sql
ALTER TABLE processing_jobs ALTER COLUMN document_version_id DROP NOT NULL;
ALTER TABLE processing_jobs ADD COLUMN checkpoint JSONB;
ALTER TABLE processing_jobs ADD CONSTRAINT ck_processing_jobs_version_required
    CHECK (job_type = 'CONFLICT_SCAN' OR document_version_id IS NOT NULL);
```

`checkpoint` holds `{"cursor_document_id": "...", "documents_total": N, "documents_scanned": N, "candidates_evaluated": N, "conflicts_created": N}` — written after each fully-processed document (document-level checkpoint granularity — see §15 for why chunk-level granularity is unnecessary complexity here). Nullable for every other job type; only `CONFLICT_SCAN` writes it.

### 9.4 Permission and audit seeding

New data migration content (same file, `013_conflicts.py`, appended after the schema DDL — mirrors migration `002`'s exact `_seed_permissions`/`_seed_system_roles` pattern):

```python
def _seed_conflict_permission() -> None:
    conn = op.get_bind()
    perm_id = str(uuid.uuid4())
    conn.execute(
        sa.text(
            "INSERT INTO permissions (id, key, description) "
            "VALUES (:id, :key, :description) ON CONFLICT (key) DO NOTHING"
        ),
        {"id": perm_id, "key": "conflict:resolve", "description": "Review, dismiss, and resolve detected conflicts"},
    )
    row = conn.execute(sa.text("SELECT id FROM permissions WHERE key = 'conflict:resolve'")).fetchone()
    actual_perm_id = row[0]
    for role_name in ("Admin", "Editor"):
        role_row = conn.execute(
            sa.text("SELECT id FROM roles WHERE name = :name AND organization_id IS NULL"),
            {"name": role_name},
        ).fetchone()
        if role_row is None:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "VALUES (:role_id, :permission_id) ON CONFLICT DO NOTHING"
            ),
            {"role_id": role_row[0], "permission_id": actual_perm_id},
        )
```

`Viewer` deliberately does not get `conflict:resolve` — mirrors `comparison:create`'s Admin/Editor-only seeding exactly (§4.2). Conflicts remain **readable** by any org member via `document:read`-tier access (no new read permission needed — `GET /conflicts*` requires only authentication + the per-conflict source-document authorization check, §18).

### 9.5 Models

`backend/app/models/conflict.py` (new file), following `app/models/document.py`'s style exactly (typed `Mapped[...]`, `mapped_column`, `TYPE_CHECKING` imports, `__repr__`):

```python
class Conflict(Base):
    __tablename__ = "conflicts"
    # columns per §9.1; relationships: organization, resolver (User), statements
    # (back_populates="conflict", cascade="all, delete-orphan", order_by=[created_at])

class ConflictStatement(Base):
    __tablename__ = "conflict_statements"
    # columns per §9.2; relationships: conflict (back_populates="statements"),
    # document, version, chunk, page (all lazy="noload")
```

---

## 10. Candidate Generation

**Objective:** for each chunk in an organization's currently-effective corpus, find plausibly-related chunks in a *different* document, without full pairwise comparison.

**Input set:** for every document in the organization, resolve its `CURRENT` version via Phase 12's `resolve_current_version(versions)` (no `as_of` — "current" as of scan time), then take all `document_chunks` for that version where `embedding IS NOT NULL`. This mirrors exactly how `resolve_allowed_documents` already builds its candidate set (`authorization_service.py:180-216`), reused conceptually, not by direct call (that function additionally applies user-specific access filtering, which a system-initiated org-wide scan does not need — the scan itself is not acting on behalf of any one user; per-conflict read-time authorization is applied later, at API read time, §18).

**Extension needed:** `DocumentChunkRepository.semantic_search()` gains one new optional parameter:

```python
async def semantic_search(
    self, *, organization_id, version_ids, query_vector, top_k=50,
    hnsw_ef_search=100, filters=None,
    exclude_document_id: str | None = None,   # NEW
) -> list[ChunkSearchResult]:
```

Implementation: when `exclude_document_id` is provided, append `AND d.id <> :exclude_document_id` to both the `WHERE` clause and `params` — one additional bound predicate, following the exact pattern `_metadata_filter_parts` already uses for Phase 8's optional filters. This is the **entire** SQL change needed; `keyword_search()` is untouched (candidate generation is semantic-only, per the roadmap — a contradiction is a meaning-level relationship, not a keyword-level one).

**Algorithm** (`ConflictService.generate_candidates(organization_id, db) -> AsyncIterator[CandidatePair]`, a generator so the worker can process incrementally):

```
for document in organization's active, non-deleted documents (ordered by id):
    current_version = resolve_current_version(document's READY versions)
    if current_version is None: continue          # no effective version yet
    for chunk in chunks of current_version, ordered by chunk_index:
        if chunk.embedding is None: continue        # not yet embedded
        results = chunk_repo.semantic_search(
            organization_id=org_id,
            version_ids=[all_current_version_ids],   # cross-document, current-only
            query_vector=chunk.embedding,
            top_k=CANDIDATE_TOP_K,                    # plan-authored: 5
            exclude_document_id=chunk's document_id,
        )
        for result in results:
            if result.similarity < CANDIDATE_SIMILARITY_THRESHOLD:  # plan-authored: 0.83
                continue
            # Dedup pair generation within one scan run: only accept the pair
            # in one direction (avoids evaluating (A,B) and (B,A) separately).
            if result.chunk_id <= chunk.id:
                continue
            yield CandidatePair(chunk_a=chunk, chunk_b=result)
```

`version_ids=[all_current_version_ids]` is precomputed once per scan run (one query resolving every document's current version, org-scoped) — not re-queried per chunk. `CANDIDATE_TOP_K = 5` and `CANDIDATE_SIMILARITY_THRESHOLD = 0.83` are named constants in `domain/conflict_rules.py`, explicitly plan-authored (no source document specifies them), chosen conservatively (roadmap risk: "start conservative — fewer, high-confidence conflicts").

**Why chunk-id string comparison for pair-ordering, not a separate seen-pairs set:** an in-memory or Redis-backed "already evaluated" set would grow unbounded across a large scan and complicate resumability. Accepting a candidate pair only when `chunk_b.id > chunk_a.id` (UUID string comparison) is the exact same "normalize the pair, evaluate once" trick Phase 12 uses for comparison-pair ordering (`(document_id, version_number)` tuple compare) — here applied to chunk IDs, since chunks (not documents) are the comparison unit.

**Cross-org isolation:** `version_ids` is built exclusively from documents in the requesting scan's `organization_id`; `semantic_search`'s existing mandatory `c.organization_id = :org_id` predicate is a second, structural enforcement layer — a candidate can never cross a tenant boundary even if the version-id list were ever built incorrectly.

---

## 11. Semantic Contradiction Verification

**When triggered:** exactly once per candidate pair surviving §10's deterministic filter (never the first filtering mechanism — embedding similarity + org/document/version filtering already does the cheap narrowing).

**Prompt** (new constants in `app/rag/prompts.py`, alongside `ANALYZER_SYSTEM_PROMPT`, same file/style as Phase 12's semantic-comparison prompt): `CONTRADICTION_CHECK_SYSTEM_PROMPT`, `CONTRADICTION_CHECK_USER_TEMPLATE`. Intent:

> "Given two statements from different documents, determine whether they assert CONTRADICTORY facts, requirements, rules, values, or obligations about the same specific point — not merely related or compatible statements about the same general topic. Respond with ONLY a JSON object: `{"is_conflict": true|false, "confidence": <0.0-1.0>, "reason": "<one sentence>", "conflict_topic": "<short label, e.g. 'Approval Timeline Requirement'>"}`."

**Call shape:** `LLMProvider.generate()` (the existing single-method interface — no new provider capability), `temperature=0.0`, small `max_tokens` (constrained JSON only), a bounded timeout (reuse `query_analyzer.py`'s `_FAST_TIMEOUT_SECONDS = 5.0` constant directly — this is the same class of "fast, structured, one-shot" call).

**Bounding input size:** truncate each statement's text sent to the LLM to **1,000 tokens** (plan-authored — smaller than Phase 12's 2,000-token comparison-truncation budget, since a single chunk is already a bounded retrieval unit, typically well under this limit; truncation here is a safety cap for pathological outlier chunks, not an expected-path behavior), reusing `app/ingestion/tokenizer.py`'s existing token-counting utility.

**Output validation:** a new parser, `parse_contradiction_check(raw: str) -> ContradictionResult | None`, in a new file `app/rag/conflict_parsing.py`, following `query_analyzer.py::parse_analyzer_output`'s exact defensive pattern (strip markdown fences, locate the JSON object, validate `is_conflict` is a bool, `confidence` is a float in `[0,1]` — clamp out-of-range values rather than reject, `conflict_topic` is a non-empty string truncated to a bounded length e.g. 80 chars matching the analyzer's `topic` truncation convention). On any parse failure, return `None` — never raise into the scan loop.

**Confidence threshold for persistence:** `CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD = 0.6` (plan-authored, named constant in `domain/conflict_rules.py`). A candidate is only persisted when `is_conflict is True AND confidence >= 0.6`. Below threshold, or `is_conflict is False`, or the parser returned `None` (any provider/parse failure) — **the candidate is simply not persisted; the scan continues to the next candidate.** This is a conservative default distinct from Phase 12's semantic-comparison failure handling (which *degrades* severity but still persists the change) — here, without a confirmed contradiction, there is nothing to persist at all; failing safe means "silently skip," never "guess."

**Non-negotiable rule (task-level, restated for the implementing agent):** the LLM's response can only ever gate whether `ConflictService` calls its own deterministic `persist_conflict(...)` method — it never writes to `conflicts`/`conflict_statements` directly, and it never decides severity, effective-date state, or deduplication. Those are entirely deterministic (§12, §14, `domain/conflict_rules.py`).

---

## 12. Effective-Date Awareness

This section governs the single riskiest failure mode in Phase 13: mistaking "a superseded document disagrees with its own successor" for "two currently-effective documents genuinely disagree."

**Reused, not reimplemented:** Phase 12's `resolve_current_version(versions, as_of=None)` and `classify_version_state(version, all_versions, today=None) -> Literal["CURRENT","SUPERSEDED","SCHEDULED"]` (`app/domain/versioning.py`) are the **only** functions that ever decide a version's temporal state anywhere in Phase 13's code. No new version-state logic is written.

**Candidate-generation-time filtering (§10):** the corpus scanned is built exclusively from each document's `CURRENT` version (via `resolve_current_version`, no `as_of`). A `SCHEDULED` or `SUPERSEDED` version's chunks are **never** even embedded into the candidate pool for the background scan — this is a stronger guarantee than "flag afterward": a not-yet-effective or no-longer-effective statement structurally cannot seed a *new* background-scan conflict in the first place.

**Why a conflict can still involve a SUPERSEDED side despite the above:** a version that was `CURRENT` at scan time can become `SUPERSEDED` later (a newer version is published, or the original version's `expiration_date` arrives) — the conflict row and its statements are historical facts as of when they were detected; they are **never retroactively deleted or edited** when a version's state later changes. This is precisely why classification must be computed live at read time, not persisted (Gap 5, §4.6).

**Live classification at read/serialization time** (`ConflictService.classify_conflict_priority(conflict, statements, db) -> Literal["ACTIVE","LIKELY_RESOLVED"]`):

```
for each statement in conflict.statements:
    state = classify_version_state(statement.version, all_versions_of_that_document)
    if state != "CURRENT":
        return "LIKELY_RESOLVED"      # any non-current side de-prioritizes the whole conflict
return "ACTIVE"                        # every statement's version is CURRENT
```

This maps directly onto FE §6.13's "Likely resolved by version update" hint and Backend §42's "flagged/de-prioritized" language. `priority` is a **derived, non-persisted field** on every `GET /conflicts`/`GET /conflicts/{id}` response — never a database column, so it is always correct as of the read, regardless of what has changed since detection.

**Comparison-derived seeding's stricter rule (§13):** unlike the background scan (which records a conflict with `priority=LIKELY_RESOLVED` if it later becomes stale), the comparison-derived trigger only fires at seed time when **both** sides are `CURRENT` at that moment (Backend §42: "across two versions that are *both* currently effective... i.e., not simply superseded"). If a qualifying `MODIFIED` change involves a non-current side, no conflict is seeded at all — there is nothing ambiguous to record; Phase 12's comparison itself already fully explains the relationship (it's a version history, not a live disagreement).

**Deterministic rule table** (plan-authored, restated for the implementing agent as the exhaustive case list):

| Statement A state | Statement B state | Background scan | Comparison-derived seed |
|---|---|---|---|
| CURRENT | CURRENT | Persist, `priority=ACTIVE` | Seed |
| CURRENT | SUPERSEDED | Persist (A's chunk not in scan pool from B's side, but B's chunk could still be A's counterpart if B was CURRENT when candidate-generated and became SUPERSEDED before persistence — rare race, harmless), `priority=LIKELY_RESOLVED` at read time | Do not seed |
| CURRENT | SCHEDULED | Not generated as a candidate (SCHEDULED excluded from candidate pool) | Do not seed |
| SUPERSEDED | SUPERSEDED | Not generated (both excluded from candidate pool) | Do not seed |
| Missing `effective_date` on either side | — | Treated as the lowest-ranked candidate per `resolve_current_version`'s existing rule (an undated version can still be `CURRENT` if it's the only READY version) — no special-casing needed; `classify_version_state` already handles `None` dates correctly (Phase 12 §8.6) | Same |

The LLM never sees or reasons about effective dates — the contradiction-check prompt (§11) asks only about semantic content. Effective-date logic is applied entirely after (candidate filtering) and independently of (priority classification) the LLM call.

---

## 13. Comparison-Derived Conflict Seeding

**Integration point (the single, minimal touch into Phase-12-owned code — §4.5 item 5):** immediately after `handle_comparison` (Phase 12's worker handler, per that plan's §12 Task 8) sets `document_comparisons.status = COMPLETED`, add one call:

```python
await ConflictService.seed_from_comparison(comparison_id=comparison.id, db=session)
```

`seed_from_comparison` re-queries `comparison_changes` by `comparison_id` (loose coupling — it does not receive in-memory change objects from the worker, just the ID; this keeps `ConflictService` fully decoupled from `handle_comparison`'s internals, and means the same method can be invoked idempotently/manually if ever needed).

**Qualification rule** (deterministic, plan-authored per Backend §42's qualitative description — restated in Backend §42's own words: "a `MODIFIED` change to a factual/numeric statement in a section flagged as 'critical'... across two versions that are *both* currently effective"):

```
for change in comparison_changes where comparison_id = :id:
    if change.change_type != "MODIFIED": continue
    if change.severity != "MAJOR": continue         # MAJOR is Phase 12's own proxy for
                                                       # "critical section OR high-materiality,
                                                       # large-proportion change" — reusing it
                                                       # here avoids re-deriving "factual/numeric"
                                                       # detection from scratch (§ below)
    version_a_state = classify_version_state(comparison.document_a_version, ...)
    version_b_state = classify_version_state(comparison.document_b_version, ...)
    if version_a_state != "CURRENT" or version_b_state != "CURRENT": continue
    if change.old_chunk_id is None or change.new_chunk_id is None: continue  # defensive; MODIFIED always has both
    seed a conflict with detection_method=COMPARISON_DERIVED, statements = [old_chunk, new_chunk]
```

**Why `severity == MAJOR` is the qualification signal, not a separate "is this factual/numeric" classifier:** Backend §42 asks for "a factual/numeric statement in a critical section" — Phase 12's own severity rule (`domain/comparison_rules.py::classify_severity`, per its plan §9.8) already promotes exactly this combination to `MAJOR` (`is_critical_section` OR `materiality == "material"` with a large `proportion_changed`). Building a second classifier here to re-derive what Phase 12's severity function already determines would be a parallel implementation of Phase 12 logic — explicitly what this plan must avoid. Using `severity == MAJOR` directly is not a perfect proxy for "factual/numeric" specifically, but it is the closest deterministic signal Phase 12 already computes, and it is conservative (MAJOR is Phase 12's *strictest* tier — fewer false positives, consistent with the roadmap's "start conservative" guidance).

**No LLM contradiction-check for this trigger.** Phase 12's comparison pipeline already established, deterministically, that the text changed materially between two currently-effective versions — that *is* the confirmation. Re-running §11's LLM call here would be redundant cost and a second, unnecessary source of false negatives (the LLM could theoretically say "not a conflict" about a change Phase 12 already classified as materially significant, which would be a confusing, hard-to-explain inconsistency between two Phase 13 code paths).

**Dedup applies identically** (§14) — `seed_from_comparison` calls the same `ConflictService.persist_or_merge(...)` entry point the background scan uses, so a comparison-derived seed and a later background-scan discovery of the same chunk pair never double-persist.

**`topic`:** derived deterministically from the change's `section` field (e.g., `"{section} — Version Discrepancy"`), never LLM-generated for this trigger (there is no LLM call in this path at all).

---

## 14. Conflict Deduplication

**Identity is chunk-membership, not topic-text similarity** (topic strings are for display only, never used to decide duplication — matching text would be fuzzy and non-deterministic, exactly what this plan must avoid per Gap 4, §4.6).

`ConflictRepository` gains two lookup methods:

```python
async def find_by_exact_pair(self, organization_id, chunk_id_a, chunk_id_b) -> Conflict | None:
    """Any conflict (ANY status — OPEN, REVIEWED, or DISMISSED) that already
    has statements for BOTH chunk_id_a and chunk_id_b."""

async def find_open_by_single_chunk(self, organization_id, chunk_id) -> Conflict | None:
    """An OPEN conflict that already has a statement for chunk_id (used to
    decide whether to GROW an existing conflict with a new statement)."""
```

`ConflictService.persist_or_merge(chunk_a, chunk_b, *, detection_result, detection_method, db) -> Conflict | None` (the single write-path entry point both triggers call):

```
existing_exact = repo.find_by_exact_pair(org_id, chunk_a.id, chunk_b.id)
if existing_exact is not None:
    return None                       # already recorded, in ANY status — never re-persist,
                                        # never reopen a resolved conflict (roadmap: "dedup
                                        # skips resolved pairs")

open_a = repo.find_open_by_single_chunk(org_id, chunk_a.id)
open_b = repo.find_open_by_single_chunk(org_id, chunk_b.id)

if open_a is not None and open_b is not None and open_a.id != open_b.id:
    # Both chunks already belong to two DIFFERENT open conflicts. Merging
    # two existing conflicts into one is out of scope for V1 (§5.2) — log
    # and skip rather than guess.
    logger.info("Candidate pair spans two distinct open conflicts — skipping (V1 does not merge).")
    return None

target = open_a or open_b
if target is not None:
    # Grow the existing OPEN conflict with whichever chunk isn't already a statement.
    new_chunk = chunk_b if open_a is not None else chunk_a
    add_statement(target, new_chunk)
    return target

# Neither chunk is known — brand-new conflict with exactly 2 statements.
return create_conflict(chunk_a, chunk_b, detection_result, detection_method)
```

**Why growth only applies to `OPEN` conflicts, never `REVIEWED`/`DISMISSED`:** Backend §42's resolution-workflow rule states resolution "is a human judgment recorded by the system, not something the backend second-guesses" and DB §26/Backend §42 both describe resolved states as terminal, "never automatically reopened." Silently adding a new disagreeing statement to an already-resolved conflict would be exactly this kind of unwanted re-litigation — if a genuinely new document later disagrees with a chunk that was part of a *resolved* conflict, `find_by_exact_pair` won't match (the new chunk was never part of that resolved conflict), so a **new**, separate conflict is created instead. This is correct: the earlier resolution is preserved untouched, and the new disagreement gets its own review cycle.

**Race conditions (concurrent workers / a background scan overlapping a comparison-derived seed):** `persist_or_merge` runs inside one DB transaction per candidate (mirroring Phase 12's per-section incremental-commit pattern, §15); the `uq_conflict_statements_conflict_chunk` unique constraint (§9.2) is the backstop — if two concurrent calls both attempt to add the same `(conflict_id, chunk_id)` pair, the loser's `INSERT` raises `IntegrityError`, which `ConflictService` catches and treats as "already added, no-op" (re-fetch and return the existing row) rather than propagating a 500 — the identical pattern Phase 12's plan uses for concurrent `document_comparisons` inserts (its §11 Task 6).

---

## 15. Background Scanning

**Trigger (V1 — Plan-authored decision, explicit MVP-vs-production split):**

- **Required for Phase 13 MVP:** a single fixed-cadence `arq.cron` entry, registered in `WorkerSettings.cron_jobs` (`app/workers/main.py`) alongside the existing `reconciliation_sweep` cron — reusing the exact same mechanism, not a new scheduler. Runs once nightly (a fixed hour, e.g. `hour={2}`, configurable via a new `settings.conflict_scan_hour` — mirrors `settings.reconciliation_sweep_interval_seconds`'s existing pattern of a settings-driven cron parameter). For each active organization with no currently in-flight (`PENDING`/`PROCESSING`/`RETRYING`) `CONFLICT_SCAN` job, create and enqueue one.
- **Production enhancement, not required for Definition of Done:** post-batch-READY triggering (roadmap's alternative trigger, "or triggered after a batch of new documents reach `READY`") and per-org configurable scan windows. Both are natural, low-risk follow-ups (the "post-batch" version is a one-line addition — enqueue a scan-check after `handle_indexing` if no scan is currently active for that org — but is explicitly deferred to keep this plan's vertical slice minimal, per the task's own instruction to favor a minimal demonstrable slice).

**Job creation:** `JobService.create_for_org_scan(db, *, organization_id, job_type=JobType.CONFLICT_SCAN) -> ProcessingJob` (new method, mirrors the shape `JobService.create_for_version` already establishes, adapted for the now-nullable `document_version_id` — leaves it `NULL` for this job type). `enqueue_after_commit` is reused completely unmodified (queue name: `QUEUE_LOW`, matching `reconciliation_sweep`'s existing "maintenance/housekeeping never starves ingestion bursts" queue assignment, per `app/infrastructure/queue.py`'s documented queue split).

**Worker dispatch (`app/workers/jobs.py`):** mirroring Phase 12's `_run_comparison_job` early-branch pattern exactly:

```python
if JobType(job.job_type) is JobType.CONFLICT_SCAN:
    return await _run_conflict_scan_job(ctx, job, session)
```

`_run_conflict_scan_job`: loads the job, transitions to `PROCESSING`, calls `ConflictService.run_scan(organization_id=job.organization_id, job=job, db=session)`, then `COMPLETED` on success or `_handle_failure`'s existing retry logic on transient error (reused unmodified — a `CONFLICT_SCAN` job failure never touches any `DocumentVersion` status, exactly like Phase 12's comparison-job failure path, so `_handle_failure`'s existing "only conditionally touch version status" adaptation already covers this case with zero further change).

**`ConflictService.run_scan(organization_id, job, db)`:**

```
checkpoint = job.checkpoint or {"cursor_document_id": None, "documents_scanned": 0,
                                  "candidates_evaluated": 0, "conflicts_created": 0}
documents = active documents for org, ordered by id, WHERE id > checkpoint.cursor_document_id (resume point)
documents_total = documents_scanned so far + count(documents remaining)

for document in documents:
    current_version = resolve_current_version(document's READY versions)
    if current_version is not None:
        for chunk in current_version's embedded chunks:
            for candidate in generate_candidates_for_chunk(chunk, org_id):     # §10
                checkpoint["candidates_evaluated"] += 1
                result = await contradiction_check(chunk, candidate)          # §11
                if result and result.is_conflict and result.confidence >= THRESHOLD:
                    conflict = await ConflictService.persist_or_merge(...)     # §14
                    if conflict is not None:
                        checkpoint["conflicts_created"] += 1
    checkpoint["cursor_document_id"] = document.id
    checkpoint["documents_scanned"] += 1
    job.checkpoint = checkpoint
    job.progress = int(100 * checkpoint["documents_scanned"] / max(documents_total, 1))
    await db.commit()   # incremental persistence per document — resumable, per §49
```

**Resumability:** identical philosophy to Phase 12's per-section incremental persistence (§49 idempotency) — a crash mid-scan resumes from `checkpoint.cursor_document_id`'s *next* document (document-level granularity, not chunk-level: re-scanning a partially-processed document from scratch on resume is a deliberate simplification — it costs at most one extra document's worth of LLM calls, and `persist_or_merge`'s dedup logic makes re-processing entirely safe, so finer-grained checkpointing buys correctness the codebase doesn't need at the cost of a more complex checkpoint schema). This is the same "resumable at a coarse, safe granularity" tradeoff Phase 12 makes explicit for its own section-level checkpointing.

**Why not a new dedicated `conflict_scan_runs` table:** the codebase's own forward-scaffolding (`JobType.CONFLICT_SCAN` already in `processing_jobs`'s `CHECK` constraint, before this plan existed) is direct evidence the original architects intended this to be a `processing_jobs` row, not a parallel tracking table. Introducing a second table to track the same concept would be exactly the kind of unnecessary-infrastructure duplication the task instructs against.

---

## 16. Conflict Resolution Workflow

**Lifecycle** (Backend §42, DB §26 — both terminal states, roadmap: "never auto-reopened"):

```
OPEN ──resolve(REVIEWED)──► REVIEWED   (terminal)
OPEN ──resolve(DISMISSED)─► DISMISSED  (terminal)
```

`ConflictService.resolve(conflict_id, *, user, decision: Literal["REVIEWED","DISMISSED"], note: str | None, db) -> Conflict`:
1. `AuthorizationService.check_permission(user=user, permission_key=PermissionKey.CONFLICT_RESOLVE, db=db)` — reuses the existing live-permission-recheck pattern exactly (`authorization_service.py:49-61`), not a new authorization mechanism.
2. Load the conflict (org-scoped via `TenantScopedRepository.get_by_id_for_org` — cross-org ID access returns `None` → `NotFoundError`, never leaks existence).
3. **Every statement's source document must be authorized for this user** (§28 security requirement) — re-verified at resolve time, not just at an earlier read, mirroring Phase 12's "re-check authorization on every access, not just creation."
4. If `conflict.status != "OPEN"` → raise the existing `ConflictError` (409) — "This conflict has already been resolved." (reusing the codebase's own generic 409 exception class, exactly as `job_service.py`'s `retry_failed_stage` already does for an analogous "wrong state for this action" case — not a new exception type).
5. Set `status`, `resolved_by = user.id`, `resolved_at = now()`, `resolution_note = note`; commit.
6. `AuditLogger.log(db, organization_id=..., user_id=user.id, action=AuditAction.CONFLICT_RESOLVED, resource_type="conflict", resource_id=conflict.id, metadata={"decision": decision, "note": note})`.
7. Return the updated conflict.

**Who can review/dismiss/resolve:** identical set — `CONFLICT_RESOLVE` (Admin/Editor), no finer-grained distinction between "reviewed" and "dismissed" (both are the same permission and the same terminal-transition mechanics; the *decision value* is what differs, not the authorization). This is a deliberate simplification versus the roadmap's illustrative FE mockup ("Not a conflict" / "Escalate to owner" / "Superseded — archive Document B" as three distinct dropdown items) — **Plan-authored decision:** those three UI options all map onto the same two backend states (`REVIEWED` covers "escalated"/"acknowledged", `DISMISSED` covers "not a conflict"); the frontend dropdown (§21) presents richer *labels* for user clarity, but the backend contract stays the two-value `decision` enum the roadmap's own API line documents (`POST /conflicts/{id}/resolve {decision, note}`), avoiding a state-machine more complex than the roadmap actually specifies.

**Reopening:** not implemented (roadmap: "reopening, if ever needed, is an explicit future human action, not an automatic transition" — out of scope; no endpoint for it).

---

## 17. Audit Logging

`app/services/audit_logger.py::AuditAction` gains one new constant, in a new section following the file's existing phase-grouped comment convention:

```python
# ── Conflict events (Phase 13) ────────────────────────────────────────────
CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
```

Written exactly once, inside `ConflictService.resolve()` (§16 step 6), via the existing `AuditLogger.log(...)` call — no new audit table, no new writer path. `metadata` carries `{"decision": ..., "note": ...}` only (never provider payloads or full statement text — matching the existing convention that audit metadata stays small and non-sensitive, e.g. `QUESTION_ASKED`'s comment: "provider payloads... are NEVER in the metadata").

---

## 18. API Design

**File:** `backend/app/api/conflicts.py` — **CREATE**, `APIRouter(prefix="/conflicts", tags=["conflicts"])`, registered in `app/main.py` alongside the existing routers (`app.include_router(conflicts_router)`, mirroring the exact `documents_router`/`ask_router` pattern at `main.py:199-225`).

| Method | Route | Auth | Permission | Request | Response | Status |
|---|---|---|---|---|---|---|
| `GET` | `/conflicts` | required | none beyond authentication (org membership; source-document authorization filters the result set, see below) | Query: `status: Optional[str]` (`OPEN`/`REVIEWED`/`DISMISSED`, default `OPEN`), `severity: Optional[str]` | `ConflictListResponse {items: list[ConflictSummary]}` | `200` |
| `GET` | `/conflicts/{conflict_id}` | required | same as above | — | `ConflictDetailResponse` (embeds `statements: list[ConflictStatementItem]`) | `200`; `404` if not found, cross-org, or any statement's source document is not authorized for this user |
| `POST` | `/conflicts/{conflict_id}/resolve` | required | `conflict:resolve` | `ConflictResolveRequest {decision: "REVIEWED"|"DISMISSED", note: Optional[str]}` | `ConflictDetailResponse` | `200`; `404` per above; `409` if already resolved; `422` if `decision` invalid |
| `GET` | `/conflicts/scan-status` | required | none beyond authentication | — | `ScanStatusResponse` | `200` |

**Source-document authorization — "no existence leakage through the conflicts API" (§28 non-negotiable requirement):** a conflict is only ever returned (list or detail) if **every** statement's source document passes the same access-level rule `AuthorizationService` already applies elsewhere (`organization` → any member; `private` → owner only; `restricted` → currently org-readable, per the existing Phase 7/16 placeholder semantics — see the note in §28). Implementation: `ConflictRepository.list_for_org(...)` performs this as a repository-level `WHERE NOT EXISTS (SELECT 1 FROM conflict_statements cs JOIN documents d ON d.id = cs.document_id WHERE cs.conflict_id = conflicts.id AND d.access_level = 'private' AND d.owner_id <> :user_id)` predicate — a single additional SQL clause in the same statement, not N separate `authorize_document_version` calls per conflict per list request (which would be correct but needlessly expensive at list scale; the per-conflict single-document authorizer *is* used, unmodified, at `GET /conflicts/{id}` and `POST /conflicts/{id}/resolve`, where the N is always small — 2 to 4 statements). A user who cannot see a `private`-source conflict gets exactly the same `200 {items: [...]}` (with that conflict simply absent) or `404` (detail) as a user who can see it but there's nothing there — structurally indistinguishable from "this conflict doesn't exist," satisfying the no-leakage requirement.

**Pagination:** `GET /conflicts` returns the full filtered list unpaginated for V1 — following the exact precedent and reasoning Phase 12's plan applies to `GET /compare/{id}/changes` (§13 of that plan): conflict volume per org is bounded by the conservative candidate/confidence thresholds (§10, §11) and is not expected to reach a scale where keyset pagination is warranted in V1. Documented as a deliberate simplification, not an oversight.

**Schemas** (`backend/app/schemas/conflict.py` — **CREATE**, `_OrmBase` convention, matching `schemas/comparison.py`'s shape from Phase 12):

```python
class ConflictStatementItem(_OrmBase):
    id: str
    document_id: str
    document_version_id: str
    document_name: str          # resolved by the service layer, not a direct passthrough
    version_number: int
    chunk_id: str
    page_number: int
    section: Optional[str] = None
    statement_text: str
    effective_date: Optional[date] = None
    version_state: Literal["CURRENT", "SUPERSEDED", "SCHEDULED"]  # computed live, §12

class ConflictSummary(_OrmBase):
    id: str
    topic: str
    severity: str
    status: str
    detection_method: str
    priority: Literal["ACTIVE", "LIKELY_RESOLVED"]   # computed live, §12
    statement_count: int
    detected_at: datetime

class ConflictDetailResponse(ConflictSummary):
    statements: list[ConflictStatementItem]
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[datetime] = None

class ConflictListResponse(BaseModel):
    items: list[ConflictSummary]

class ConflictResolveRequest(BaseModel):
    decision: Literal["REVIEWED", "DISMISSED"]
    note: Optional[str] = None

class ScanStatusResponse(BaseModel):
    last_scan_status: Optional[str] = None       # PENDING/PROCESSING/RETRYING/COMPLETED/FAILED
    last_scan_completed_at: Optional[datetime] = None
    last_scan_conflicts_created: Optional[int] = None
    unscanned_document_count: int                 # docs created after last completed scan
```

`unscanned_document_count`: `COUNT(documents WHERE created_at > last_completed_scan.completed_at)` (or all active documents if no scan has ever completed) — feeds the FE §6.13 "3 recently uploaded documents haven't been scanned yet" partial banner, computed at read time, never stored.

---

## 19. CONFLICT_DETECTION Integration

**Location:** `app/services/ask_service.py`, the same branch point Phase 12 adds for `COMPARISON`/`CHANGE_DETECTION` (that plan's §14, `ask_stream` around line 268) — this plan adds a sibling `elif` immediately after Phase 12's branch:

```python
analysis = await analyze_query(question)
if analysis.intent in ("COMPARISON", "CHANGE_DETECTION"):
    ...  # Phase 12, unmodified
elif analysis.intent == "CONFLICT_DETECTION":
    accessible_doc_ids = await AuthorizationService.resolve_allowed_documents(user, db, scope=scope)
    conflicts = await ConflictService.list_for_chat(
        organization_id=user.organization_id,
        accessible_document_ids=accessible_doc_ids,
        topic_hint=analysis.scope_hints,   # advisory only, per Backend §27 — narrows, never expands
        db=db,
    )
    if not conflicts:
        # deterministic, non-fabricated response — no LLM call needed to say "none found"
        return <ASSISTANT message: "I didn't find any recorded conflicts in the documents you have access to.">
    narration = await narrate_conflicts(conflicts, provider=llm)   # constrained, phrasing-only
    # persist as a normal ASSISTANT Message with Citations (one per statement referenced),
    # via the EXISTING message+citation persistence path — unmodified
else:
    ...  # existing standard RAG path, unchanged
```

**`ConflictService.list_for_chat`:** filters persisted `OPEN` conflicts to those whose **every** statement's `document_id` is in `accessible_doc_ids` (the same authorization principle as §18's list endpoint, applied here using the conversation's already-resolved scope rather than a fresh authorization call — `accessible_doc_ids` is exactly what `resolve_allowed_documents` already computed for the retrieval step of the same request, reused rather than recomputed). If `topic_hint` is non-empty, additionally filter to conflicts whose `topic` contains any hint substring (case-insensitive) — simple, deterministic keyword overlap, never an LLM relevance judgment.

**`narrate_conflicts`** (new function, `app/rag/conflict_parsing.py` or a new `app/rag/conflict_narration.py` — mirrors Phase 12's `narrate_changes` exactly): input = the already-fetched, already-filtered conflict list (topic, severity, statements' text/document names); output = natural-language prose only, explicitly forbidden by its system prompt from asserting any conflict not present in the provided list (same "narrate, never originate" philosophy as `citation_validator.py::check_entailment()` and Phase 12's `narrate_changes`). **This satisfies the task's non-negotiable rule directly: the conversational layer orchestrates and narrates persisted results — it never independently detects a new conflict.**

**Citations for the narrated answer:** one `Citation` row per statement the narration references, `chunk_id = statement.chunk_id` — reuses the existing atomic message+citation persistence path completely unmodified (identical mechanics to Phase 12's `CHANGE_DETECTION` narration citations).

---

## 20. Inline RAG Conflict Surfacing

**Where this hook lives:** `AskService.ask_stream`, immediately after the retrieval stage (`retrieval = await retriever.search(...)`, `ask_service.py:280-287`) and before generation begins — for **every** intent, not just `CONFLICT_DETECTION` (this is the "sources disagree" case, distinct from a user explicitly asking about conflicts).

```python
retrieved_chunk_ids = [r.chunk_id for r in retrieval.results]
conflicts_among_sources = await ConflictService.find_conflicts_among_chunks(
    organization_id=user.organization_id,
    chunk_ids=retrieved_chunk_ids,
    db=self._db,
)
```

**`ConflictService.find_conflicts_among_chunks`:** a single deterministic query — `SELECT conflict_id FROM conflict_statements WHERE chunk_id = ANY(:retrieved_chunk_ids) GROUP BY conflict_id HAVING COUNT(DISTINCT chunk_id) >= 2`, joined to `conflicts` filtered to `status = 'OPEN'` and `organization_id = :org_id`. This finds any `OPEN` conflict where **at least two of the chunks retrieved for this specific question** are already known, persisted statements of the same conflict — i.e., the retrieval itself surfaced disagreeing evidence.

**Why no additional authorization check is needed here (the one place in this plan authorization is *not* independently re-verified):** `retrieval.results` was already produced by `HybridRetriever.search()`, which is itself fully permission-scoped (`resolve_allowed_documents` gates every chunk it can return). A conflict detected purely among chunks the user's own retrieval already surfaced cannot, by construction, expose any chunk the user was not already independently authorized to see. This is the one case in the entire plan where reusing an *already-enforced* upstream authorization boundary is strictly sufficient — stated explicitly here so an implementing agent does not mistakenly add a redundant (or, worse, a subtly different and therefore inconsistent) authorization check at this specific point.

**Passing this to generation and the response:** `AskOutcome`/`AskStreamEvent` (`ask_service.py`) gain a new optional field, `conflicts: list[ConflictNotice]` (small struct: `conflict_id`, `topic`, `severity`), populated deterministically from the query above — **never from LLM output.** Two effects:
1. A new SSE event type, `conflict_notice` (sibling to the existing citation-related events), carrying this structured data — the frontend renders the ⚠ banner from this event, not from parsing generated text.
2. Optionally, a short, fully deterministic system-note is appended to the generation prompt's context (e.g., `"Note: the retrieved sources disagree on '{topic}'. Acknowledge this disagreement in your answer without resolving it or picking a side."`) — the *content* of this note (topic, existence) is deterministic and app-supplied; only the model's *prose acknowledgment* is generative, exactly matching Phase 12's `CHANGE_DETECTION` narration philosophy applied to a hint rather than a full answer.

**Preventing hallucinated conflicts (task's explicit requirement):** because the notice is populated exclusively from the DB query above and rendered by the frontend from the structured `conflict_notice` event (not extracted from generated prose), the LLM has no path to *invent* a conflict notice that wasn't already true — even if the model's prose fails to mention the disagreement, or over-states it, the notice UI element itself is 100% deterministic.

---

## 21. Frontend Implementation

**Blocking prerequisite:** `frontend/src/lib/` must exist and the app must build — this is Phase 12 Task 13's responsibility (§4.1); every task below assumes it is already done.

**New files (all CREATE):**
- `frontend/src/lib/api/conflicts.ts` — `listConflictsApi`, `getConflictApi`, `resolveConflictApi`, `getConflictScanStatusApi`, matching §18's actual contract (snake_case JSON, matching every existing schema — same convention Phase 12's `comparison.ts` establishes).
- `frontend/src/hooks/queries/useConflicts.ts` — `useConflicts({status, severity})`, `useConflict(id)`, `useResolveConflict()` (mutation, invalidates the list + detail query keys on success), `useConflictScanStatus()`.
- `frontend/src/features/conflicts/index.ts` — barrel.
- `frontend/src/features/conflicts/ConflictsPage.tsx` — the `/app/conflicts` route: list of `ConflictCard`s, status tab filter (Open/Reviewed/Dismissed — mirroring FE §6.13's "Resolved conflicts move to a 'Reviewed' tab"), severity filter, empty state ("No conflicts detected across your knowledge base" + last-scan timestamp from `useConflictScanStatus()`), partial banner (unscanned document count).
- `frontend/src/features/conflicts/ConflictDetailPage.tsx` — the `/app/conflicts/:id` route: full statement list, effective-date/version-state badges per statement (using the API's live-computed `version_state`), "Compare Sources" action (§22), resolution menu.
- `frontend/src/features/conflicts/ConflictCard.tsx` — per FE §6.13's exact mockup: severity badge, topic, per-statement document name + effective date + superseded de-emphasis, citation-style source display, "Compare Sources" + "Mark as Reviewed ▾" actions (permission-gated — hidden/disabled for a Viewer, per FE §6.13's explicit UX-state requirement).
- `frontend/src/features/conflicts/ConflictResolutionMenu.tsx` — accessible menu (`role="menu"`, keyboard navigation per FE §6.13 Accessibility), presenting the richer labels ("Not a conflict", "Escalate to owner", "Superseded — archive") that map onto the two-value `decision` payload (§16's plan-authored simplification) plus a note text field.
- `frontend/src/features/conflicts/ConflictSeverityBadge.tsx` — reuse the exact visual pattern Phase 12's `ChangeSeverityBadge.tsx` establishes (same three-tier MAJOR/MODERATE/MINOR styling) rather than a second badge component with divergent styling.
- `frontend/src/features/conflicts/conflicts.css` — styling, following `ask.css`/`processing.css`'s existing conventions.
- `frontend/src/App.tsx` — **MODIFY**: add two new routes, `<Route path="conflicts" element={<ConflictsPage />} />` and `<Route path="conflicts/:id" element={<ConflictDetailPage />} />` (there is no existing placeholder to replace here — these are net-new routes, unlike Phase 12's `/compare` routes); wire the placeholder Dashboard's "Open Conflicts" KPI card (`App.tsx:64`) to `useConflictScanStatus()`/a lightweight open-conflict count query, as a link to `/app/conflicts`.
- `frontend/src/features/documents/TocPanel.tsx` — **MODIFY**: accept a new optional prop `conflictSectionIds?: Set<string>`; render the `[•]` marker (FE §6.5) on any `TocNode` whose id (or matching section) is present in the set — the marker is a `<span className="toc-conflict-marker" aria-label="Unresolved conflict in this section">•</span>`, appended next to the existing `toc-pages` span. **Data source:** a lightweight new hook, `useDocumentConflictSections(documentId)`, backed by a query against `GET /conflicts?status=open` filtered client-side to statements whose `document_id` matches the current document (V1 — the conflicts list is not expected to be large per §18's pagination reasoning, so client-side filtering is acceptable; a dedicated `GET /conflicts?document_id=` server-side filter is a straightforward follow-up if list volume ever grows, explicitly not required for Phase 13's Definition of Done).
- `frontend/src/features/ask/CitedAnswer.tsx` (or wherever the SSE stream's message rendering lives) — **MODIFY**: handle the new `conflict_notice` SSE event type; render the ⚠ "Conflicting information detected" banner (per the roadmap's own mockup) with "Compare Sources"/"Review Conflict" links routing to `/app/conflicts/:id` and `/app/compare/:comparisonId` respectively (Compare Sources needs a comparison to exist for the two statement documents — if none exists yet, the link instead routes to `/app/compare?documentA=&documentB=` pre-filled, using Phase 12's `ComparisonPicker`, which creates one on demand).

**Explicitly not built:** a redesigned Dashboard, a redesigned Analytics page, any client-side conflict "detection" (all conflict data is server-computed and persisted — the frontend only ever renders what the API returns, never independently infers a conflict from two pieces of text it happens to have loaded).

---

## 22. Compare Sources Integration

**No new diff/comparison code is written for Phase 13.** `ConflictDetailPage`'s "Compare Sources" action:
1. Reads the conflict's first two statements' `document_a`/`document_b` (`document_id` + `document_version_id`).
2. Calls Phase 12's existing `createComparisonApi({document_a_version_id, document_b_version_id})` (reuse-checked — if a comparison for this exact pair already exists, Phase 12's own reuse logic returns it instantly, no recomputation).
3. Navigates to `/app/compare/:comparisonId`, Phase 12's existing `ComparisonResults` page — **section-anchored**, per FE §6.13's requirement ("opens the full Comparison view pre-loaded with both documents, section-anchored to the conflicting passage"): pass the conflict statement's `section` as a query param (`?section=...`) that `ComparisonResults`/`SectionNavigator` (Phase 12) reads to auto-scroll/select that section on load — this is the **one, small, additive extension** to Phase-12-owned frontend code Phase 13 makes (`SectionNavigator.tsx` gains an optional `initialSection` prop read from the URL). Everything else about the comparison view — `DiffViewer`, `ChangeCard`, severity breakdown, "View Sources" — is used exactly as Phase 12 built it, unmodified.

**N > 2 statements:** for a conflict with three or more statements, "Compare Sources" on a specific statement pair (e.g., clicking a specific two-statement row within the detail view) passes that pair's two version IDs — the same mechanism, just parameterized per pair rather than always "first two." No new UI concept is introduced; it is the same action, invoked per adjacent pair.

---

## 23. Detailed Implementation Tasks

### Task 0 — Verify Phase 12 is actually complete (prerequisite gate, not a Phase 13 deliverable)

**Objective:** confirm, before starting Task 1, that `PHASE-12-IMPLEMENTATION-PLAN.md`'s Definition of Done (§23) is met — not merely "started."
**Files:** none written; this is a verification checklist run against the live repository.
**Checks:** Alembic head is `012`; `backend/app/models/comparison.py`, `services/comparison_service.py`, `api/compare.py` exist; `backend/app/domain/versioning.py::resolve_current_version()` no longer references `is_current`; `classify_version_state()` exists in the same file; `frontend/src/lib/api/client.ts` exists and `npm run build` succeeds in `frontend/`; `frontend/src/App.tsx`'s two `/app/compare*` routes render real components, not `PlaceholderPage`; Phase 12's full test suite (`backend/tests/**/test_comparison*.py`, `test_chat_change_detection.py`) passes.
**Acceptance criteria:** every check above passes. If any fails, stop and complete the corresponding Phase 12 step first — do not begin Phase 13 database or service work against an incomplete foundation.
**Tests:** none (this task *runs* Phase 12's own test suite as its acceptance gate).

### Task 1 — Domain layer: conflict rules, shared critical-section helper

**Files:**
- `backend/app/domain/conflict_rules.py` — **CREATE**: `classify_conflict_severity(*, is_critical_section: bool, confidence: float, statement_count: int) -> Literal["MAJOR","MODERATE","MINOR"]` per the rule below; named constants `CANDIDATE_TOP_K = 5`, `CANDIDATE_SIMILARITY_THRESHOLD = 0.83`, `CONFIRMED_CONFLICT_CONFIDENCE_THRESHOLD = 0.6`, `CONTRADICTION_CHECK_TOKEN_BUDGET = 1000`.
- `backend/app/domain/comparison_rules.py` — **MODIFY** (Phase-12-owned file): extract its inline `is_critical_section` lookup (per Phase 12 plan §9.8) into a standalone, exported function `is_critical_section(section_title: str | None, section_number: str | None, org_settings: dict) -> bool`, called from both `classify_severity` (existing Phase 12 caller, behavior unchanged) and the new `ConflictService` (§Task 4).

```python
def classify_conflict_severity(*, is_critical_section: bool, confidence: float, statement_count: int) -> str:
    if is_critical_section:
        return "MAJOR"
    if statement_count >= 3:
        return "MAJOR" if confidence >= 0.75 else "MODERATE"
    if confidence >= 0.85:
        return "MODERATE"
    return "MINOR"
```

**Dependencies:** Task 0 (needs Phase 12's `domain/comparison_rules.py` to exist to extract from).
**Acceptance criteria:** 100% branch coverage in `backend/tests/unit/test_conflict_rules.py`; `is_critical_section` extraction does not change any existing Phase 12 `test_comparison_rules.py` test's outcome (pure refactor, verified by running that suite unmodified after the extraction).
**Tests:** `backend/tests/unit/test_conflict_rules.py` (new).

### Task 2 — Database: migration, models

**Files:**
- `backend/alembic/versions/013_conflicts.py` — **CREATE**: full DDL per §9.1–9.4 (`conflicts`, `conflict_statements`, `processing_jobs` extension, permission/role seeding), `down_revision = "012"`.
- `backend/app/models/conflict.py` — **CREATE**: `Conflict`, `ConflictStatement` per §9.5.
- `backend/app/models/processing_job.py` — **MODIFY**: `document_version_id: Mapped[Optional[str]]`, `nullable=True`; add `checkpoint: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)`.
- `backend/app/domain/permissions.py` — **MODIFY**: add `CONFLICT_RESOLVE = "conflict:resolve"` to `PermissionKey`.
- `backend/app/services/audit_logger.py` — **MODIFY**: add `AuditAction.CONFLICT_RESOLVED = "CONFLICT_RESOLVED"`.
- `backend/alembic/env.py` — **MODIFY**: import `Conflict`, `ConflictStatement`.
- `backend/tests/conftest.py` — **MODIFY**: add `"conflict_statements"`, `"conflicts"` to `_CLEANUP_TABLES` (FK-safe order, before `document_versions`/`documents`/`users`).

**Dependencies:** Task 0, Task 1 (for constant references in comments only — no hard code dependency).
**Acceptance criteria:** `alembic upgrade head` reaches `013` cleanly on a DB already at `012`; `downgrade -1` reverses cleanly; `conflicts`/`conflict_statements` CHECK constraints reject invalid `status`/`severity`/`detection_method`; `ck_conflicts_resolution_fields` rejects an `OPEN` row with `resolved_by` set and rejects a `REVIEWED` row with `resolved_by IS NULL`; `ck_processing_jobs_version_required` rejects a non-`CONFLICT_SCAN` job with `document_version_id IS NULL` and allows a `CONFLICT_SCAN` job with it `NULL`; `conflict:resolve` permission exists and is granted to Admin/Editor, not Viewer (assert via a query, mirroring `test_permissions.py`'s existing style).
**Tests:** extend `backend/tests/integration/test_migrations.py`.

### Task 3 — `semantic_search()` extension

**Files:** `backend/app/repositories/document_chunk_repository.py` — **MODIFY**: add `exclude_document_id: str | None = None` parameter and the corresponding `AND d.id <> :exclude_document_id` predicate (§10).
**Dependencies:** none.
**Acceptance criteria:** existing `semantic_search` callers (Phase 7/8 retrieval, unmodified) behave identically when the new parameter is omitted; a new unit/integration test confirms a chunk from the excluded document never appears in results even when it would otherwise rank first.
**Tests:** extend `backend/tests/integration/test_hybrid_search_pipeline.py` or add a focused case to a new `test_conflict_candidate_generation.py` integration test (§27.2).

### Task 4 — `ConflictRepository`

**File:** `backend/app/repositories/conflict_repository.py` — **CREATE**, extends `TenantScopedRepository[Conflict]`.

Methods: `create(organization_id, topic, severity, detection_method) -> Conflict`; `add_statement(conflict_id, **fields) -> ConflictStatement`; `find_by_exact_pair(organization_id, chunk_id_a, chunk_id_b) -> Conflict | None`; `find_open_by_single_chunk(organization_id, chunk_id) -> Conflict | None`; `get_by_id_for_org(...)` (inherited); `resolve(conflict, *, status, resolved_by, resolution_note) -> Conflict`; `list_for_org(organization_id, *, status, severity, requesting_user_id) -> list[Conflict]` (with the private-document exclusion predicate, §18); `list_by_document_id(...)` (for the TOC-marker frontend query, §21) — **or**, per §21's stated V1 simplification, this can be served entirely by `list_for_org` plus client-side filtering; implement the dedicated method only if profiling later shows it's needed (not required for Definition of Done).

**Dependencies:** Task 2.
**Acceptance criteria:** unit/integration tests (following `test_repositories.py`'s precedent, per Phase 12's Task 5) cover exact-pair hit/miss, single-chunk open-lookup, and the private-document exclusion filter.
**Tests:** `backend/tests/integration/test_repositories.py` (extend) or a new `test_conflict_repository.py` alongside it.

### Task 5 — Candidate generation

**File:** `backend/app/services/conflict_service.py` — **CREATE** (this and Tasks 6–9 all add to this one file, following `ComparisonService`'s single-file-per-domain-service convention): `ConflictService.generate_candidates(organization_id, db) -> AsyncIterator[CandidatePair]` per §10.
**Dependencies:** Task 3, Task 1.
**Acceptance criteria:** org-scoped (never crosses tenants — assert via a two-org fixture); excludes same-document pairs; excludes `SUPERSEDED`/`SCHEDULED`-version chunks from the pool; each unordered chunk pair yielded at most once per call; respects `CANDIDATE_TOP_K`/`CANDIDATE_SIMILARITY_THRESHOLD`.
**Tests:** `backend/tests/unit/test_conflict_candidate_generation.py` (pure-logic assertions against a stubbed repository) plus an integration test against the real DB with `StubEmbeddingProvider`.

### Task 6 — Contradiction verification + parsing

**Files:**
- `backend/app/rag/prompts.py` — **MODIFY**: add `CONTRADICTION_CHECK_SYSTEM_PROMPT`, `CONTRADICTION_CHECK_USER_TEMPLATE` per §11.
- `backend/app/rag/conflict_parsing.py` — **CREATE**: `parse_contradiction_check(raw: str) -> ContradictionResult | None` per §11.
- `backend/app/services/conflict_service.py` — **MODIFY** (adds to Task 5's file): `ConflictService.check_contradiction(chunk_a, chunk_b, *, provider) -> ContradictionResult | None`.

**Dependencies:** Task 5.
**Acceptance criteria:** happy path (valid JSON, `is_conflict=true`); `is_conflict=false`; malformed JSON → `None`; out-of-range confidence clamped, not rejected; provider timeout/error → `None`, never raises into the caller.
**Tests:** `backend/tests/unit/test_conflict_parsing.py` (mirrors `test_query_analyzer.py`'s parser-testing style, using `StubLLMProvider`).

### Task 7 — Persistence, dedup/merge

**File:** `backend/app/services/conflict_service.py` — **MODIFY**: `ConflictService.persist_or_merge(...)` per §14; `ConflictService.classify_conflict_priority(...)` per §12.
**Dependencies:** Task 4, Task 1, Task 6.
**Acceptance criteria:** covers §16.4's Persistence Tests matrix in full (§27.2) — exact-pair dedup across all statuses; single-chunk growth only for `OPEN`; two-distinct-open-conflicts-collision skip; concurrent-insert race handled via `IntegrityError` catch-and-refetch.
**Tests:** `backend/tests/integration/test_conflict_dedup.py` (new).

### Task 8 — Background scan worker + cron

**Files:**
- `backend/app/services/job_service.py` — **MODIFY**: add `create_for_org_scan(...)` per §15.
- `backend/app/workers/jobs.py` — **MODIFY**: `_run_conflict_scan_job` dispatch branch (mirrors Phase 12's `_run_comparison_job`); `ConflictService.run_scan(...)` orchestration (adds to Task 5's file) implementing the checkpointed loop in §15.
- `backend/app/workers/main.py` — **MODIFY**: add the nightly `arq.cron` entry to `WorkerSettings.cron_jobs`, alongside `reconciliation_sweep`.
- `backend/app/core/config.py` — **MODIFY**: add `conflict_scan_hour: int = 2` (or equivalent settings field feeding the cron's `hour=` parameter), following `reconciliation_sweep_interval_seconds`'s existing settings-driven-cron pattern.

**Dependencies:** Tasks 5, 6, 7.
**Acceptance criteria:** full scan run against a seeded fixture produces the expected `conflicts`/`conflict_statements` rows; crash-and-resume (simulate by raising after N of M documents processed) resumes from `checkpoint.cursor_document_id` without duplicating already-created conflicts; retry-exhaustion marks the job `FAILED` with `error_message` populated (mirrors Phase 12's `handle_comparison` retry-exhaustion test); a second concurrent scan for the same org is prevented at the trigger level (§15 — "no currently in-flight job" check before creating a new one).
**Tests:** `backend/tests/integration/test_conflict_scan_pipeline.py` (new, mirrors Phase 12's `test_comparison_pipeline.py` structure).

### Task 9 — Comparison-derived seeding hook

**Files:**
- `backend/app/services/conflict_service.py` — **MODIFY**: `ConflictService.seed_from_comparison(comparison_id, db)` per §13.
- `backend/app/workers/jobs.py` — **MODIFY** (Phase-12-owned file, one-line addition per §4.5 item 5): call `seed_from_comparison` immediately after `document_comparisons.status = COMPLETED` inside `handle_comparison`.

**Dependencies:** Task 7 (shares `persist_or_merge`), Task 0 (Phase 12's `handle_comparison` must exist).
**Acceptance criteria:** a `MODIFIED`+`MAJOR` change between two `CURRENT` versions seeds a conflict with `detection_method='COMPARISON_DERIVED'`; a `MODIFIED`+`MODERATE`/`MINOR` change does not seed; a qualifying change where one side is `SUPERSEDED` does not seed; re-running the same comparison (already reuse-checked by Phase 12, so `handle_comparison` never re-runs for an existing pair) never double-seeds.
**Tests:** `backend/tests/integration/test_conflict_comparison_seeding.py` (new).

### Task 10 — Resolution workflow + audit

**File:** `backend/app/services/conflict_service.py` — **MODIFY**: `ConflictService.resolve(...)` per §16, wired to `AuditLogger`/`AuditAction.CONFLICT_RESOLVED` per §17.
**Dependencies:** Task 2 (permission/audit constants), Task 4.
**Acceptance criteria:** `OPEN → REVIEWED`/`OPEN → DISMISSED` both succeed and audit; resolving an already-resolved conflict raises `ConflictError` (409); resolving without `conflict:resolve` raises `InsufficientPermissionsError` (403); resolving when not all statement-source documents are authorized raises `NotFoundError` (404, no leakage).
**Tests:** `backend/tests/unit/test_conflict_resolution.py` (state-machine assertions) + covered again at the API layer (Task 11).

### Task 11 — API layer

**Files:**
- `backend/app/api/conflicts.py` — **CREATE**: the four endpoints per §18.
- `backend/app/schemas/conflict.py` — **CREATE**: schemas per §18.
- `backend/app/main.py` — **MODIFY**: register `conflicts_router`.

**Dependencies:** Task 4, Task 7, Task 10.
**Acceptance criteria:** §27.3/§27.4's full matrix passes.
**Tests:** `backend/tests/api/test_conflicts.py` (new).

### Task 12 — `CONFLICT_DETECTION` chat routing

**Files:**
- `backend/app/services/conflict_service.py` — **MODIFY**: `ConflictService.list_for_chat(...)` per §19.
- `backend/app/rag/prompts.py` — **MODIFY**: add `CONFLICT_NARRATION_SYSTEM_PROMPT`/`_USER_TEMPLATE`.
- `backend/app/rag/conflict_narration.py` — **CREATE**: `narrate_conflicts(...)`.
- `backend/app/services/ask_service.py` — **MODIFY**: add the `CONFLICT_DETECTION` branch (§19), alongside Phase 12's `COMPARISON`/`CHANGE_DETECTION` branch.

**Dependencies:** Task 4, Task 0 (needs Phase 12's intent-branch location already in place — this task inserts an `elif` next to it, not before it).
**Acceptance criteria:** "What conflicts exist between our HR policies?" with ≥1 accessible open conflict returns a narrated, citation-backed answer; zero accessible conflicts returns the deterministic "none found" message (no LLM call for that case); a conflict backed by an inaccessible document is never mentioned (verified via a cross-permission fixture).
**Tests:** `backend/tests/integration/test_chat_conflict_detection.py` (new, mirrors Phase 12's `test_chat_change_detection.py`).

### Task 13 — Inline RAG surfacing

**Files:**
- `backend/app/services/conflict_service.py` — **MODIFY**: `ConflictService.find_conflicts_among_chunks(...)` per §20.
- `backend/app/services/ask_service.py` — **MODIFY**: `AskOutcome`/`AskStreamEvent` gain `conflicts: list[ConflictNotice]`; the post-retrieval hook per §20; the deterministic system-note injection into generation context.

**Dependencies:** Task 4.
**Acceptance criteria:** a question whose retrieval surfaces ≥2 chunks from the same `OPEN` conflict emits a `conflict_notice` SSE event with correct `conflict_id`/`topic`/`severity`; a question with no overlapping conflict statements emits no such event; the notice never appears for a `REVIEWED`/`DISMISSED` conflict.
**Tests:** `backend/tests/integration/test_ask_conflict_surfacing.py` (new).

### Task 14 — Frontend: API client + hooks

**Files:** `frontend/src/lib/api/conflicts.ts`, `frontend/src/hooks/queries/useConflicts.ts` — **CREATE**, per §21.
**Dependencies:** Phase 12 Task 13 (`lib/` must exist); Task 11 (real API contract).
**Acceptance criteria:** typed against the exact `§18` response shapes; `npm run build` succeeds.
**Tests:** none required beyond the build check (this codebase has no frontend unit-test runner configured — consistent with Phase 12's frontend tasks, which are verified manually via the `run` skill, not automated tests).

### Task 15 — Frontend: Conflicts view, ConflictCard, resolution menu

**Files:** `frontend/src/features/conflicts/*` — **CREATE**, per §21. `frontend/src/App.tsx` — **MODIFY**: add the two new routes + wire the Dashboard KPI.
**Dependencies:** Task 14.
**Acceptance criteria:** manually verified end-to-end (via the `run` skill) against the seeded fixture (§27.7): `/app/conflicts` lists the seeded conflict with correct severity/topic; detail view shows both statements with correct effective-date/version-state badges; "Mark as Reviewed"/"Dismiss" (Editor/Admin session) transitions the conflict and it moves to the Reviewed tab; the same actions are hidden/disabled for a Viewer session.
**Tests:** manual verification only (per the note in Task 14).

### Task 16 — Frontend: TOC markers, Compare Sources, inline chat notice

**Files:** `frontend/src/features/documents/TocPanel.tsx` — **MODIFY**; `frontend/src/features/conflicts/ConflictDetailPage.tsx` "Compare Sources" wiring — per §22; Phase-12-owned `SectionNavigator.tsx` — **MODIFY** (add `initialSection` prop, §22); `frontend/src/features/ask/CitedAnswer.tsx` (or the equivalent SSE-message component) — **MODIFY**, per §21's `conflict_notice` handling.
**Dependencies:** Task 15, Phase 12's Comparison UI (Task 0 verifies it exists).
**Acceptance criteria:** manually verified: a document with an open conflict in one of its sections shows the `[•]` marker in its TOC; clicking "Compare Sources" from a `ConflictCard` opens the Comparison view already scrolled to the correct section; asking a chat question that retrieves conflicting sources shows the ⚠ banner with working links.
**Tests:** manual verification only.

---

## 24. File-Level Change Map

### Backend

```
File: backend/alembic/versions/013_conflicts.py
Action: CREATE
Purpose: conflicts, conflict_statements tables; processing_jobs extension; permission/role seeding.
Changes: See §9 in full.
Dependencies: down_revision="012" (Phase 12's migration must exist).

File: backend/alembic/env.py
Action: MODIFY
Purpose: Register Conflict, ConflictStatement for autogenerate parity.
Dependencies: models/conflict.py.

File: backend/app/models/conflict.py
Action: CREATE
Purpose: SQLAlchemy models for Conflict, ConflictStatement.
Changes: See §9.5.
Dependencies: none.

File: backend/app/models/processing_job.py
Action: MODIFY
Purpose: document_version_id nullable; add checkpoint JSONB column.
Changes: See §9.3.
Dependencies: none.

File: backend/app/domain/permissions.py
Action: MODIFY
Purpose: Add PermissionKey.CONFLICT_RESOLVE.
Dependencies: none.

File: backend/app/domain/conflict_rules.py
Action: CREATE
Purpose: classify_conflict_severity(), candidate/confidence threshold constants.
Changes: See §11, §23 Task 1.
Dependencies: none.

File: backend/app/domain/comparison_rules.py
Action: MODIFY (Phase-12-owned file)
Purpose: Extract is_critical_section() into a standalone, reusable function.
Changes: See §23 Task 1.
Dependencies: Phase 12 must have created this file first.

File: backend/app/repositories/conflict_repository.py
Action: CREATE
Purpose: DB access for conflicts/conflict_statements; dedup lookups.
Changes: See §14, §23 Task 4.
Dependencies: models/conflict.py.

File: backend/app/repositories/document_chunk_repository.py
Action: MODIFY
Purpose: semantic_search() gains exclude_document_id parameter.
Changes: See §10, §23 Task 3.
Dependencies: none.

File: backend/app/services/conflict_service.py
Action: CREATE
Purpose: candidate generation, contradiction verification, dedup/merge, scan
         orchestration, comparison-derived seeding, resolution, chat/inline
         query methods — the single Phase 13 domain service.
Changes: See §10-§20, §23 Tasks 5-13.
Dependencies: conflict_repository.py, domain/conflict_rules.py,
              domain/comparison_rules.py, authorization_service.py.

File: backend/app/services/job_service.py
Action: MODIFY
Purpose: create_for_org_scan().
Changes: See §15, §23 Task 8.
Dependencies: models/processing_job.py.

File: backend/app/services/audit_logger.py
Action: MODIFY
Purpose: Add AuditAction.CONFLICT_RESOLVED.
Dependencies: none.

File: backend/app/services/ask_service.py
Action: MODIFY
Purpose: CONFLICT_DETECTION intent branch; inline conflict-surfacing hook.
Changes: See §19, §20, §23 Tasks 12-13.
Dependencies: conflict_service.py.

File: backend/app/workers/jobs.py
Action: MODIFY
Purpose: CONFLICT_SCAN dispatch branch + handler; comparison-derived seeding
         hook call inside handle_comparison (Phase-12-owned function).
Changes: See §15, §13, §23 Tasks 8-9.
Dependencies: conflict_service.py, job_service.py.

File: backend/app/workers/main.py
Action: MODIFY
Purpose: Register the nightly conflict-scan arq.cron entry.
Changes: See §15, §23 Task 8.
Dependencies: workers/jobs.py.

File: backend/app/core/config.py
Action: MODIFY
Purpose: Add conflict_scan_hour (or equivalent) setting.
Dependencies: none.

File: backend/app/api/conflicts.py
Action: CREATE
Purpose: GET /conflicts, GET /conflicts/{id}, POST /conflicts/{id}/resolve,
         GET /conflicts/scan-status.
Changes: See §18, §23 Task 11.
Dependencies: conflict_service.py, schemas/conflict.py.

File: backend/app/main.py
Action: MODIFY
Purpose: Register conflicts_router.
Dependencies: api/conflicts.py.

File: backend/app/schemas/conflict.py
Action: CREATE
Purpose: Pydantic schemas for the conflicts API.
Changes: See §18.
Dependencies: none.

File: backend/app/rag/prompts.py
Action: MODIFY
Purpose: Contradiction-check and conflict-narration prompt templates.
Changes: See §11, §19.
Dependencies: none.

File: backend/app/rag/conflict_parsing.py
Action: CREATE
Purpose: parse_contradiction_check() — defensive JSON parsing.
Changes: See §11, §23 Task 6.
Dependencies: none.

File: backend/app/rag/conflict_narration.py
Action: CREATE
Purpose: narrate_conflicts() — constrained narration LLM call.
Changes: See §19, §23 Task 12.
Dependencies: rag/prompts.py.
```

### Backend Tests

```
File: backend/tests/unit/test_conflict_rules.py — CREATE — §23 Task 1
File: backend/tests/unit/test_conflict_candidate_generation.py — CREATE — §23 Task 5
File: backend/tests/unit/test_conflict_parsing.py — CREATE — §23 Task 6
File: backend/tests/unit/test_conflict_resolution.py — CREATE — §23 Task 10
File: backend/tests/integration/test_repositories.py — MODIFY (or new test_conflict_repository.py) — §23 Task 4
File: backend/tests/integration/test_conflict_dedup.py — CREATE — §23 Task 7
File: backend/tests/integration/test_conflict_scan_pipeline.py — CREATE — §23 Task 8
File: backend/tests/integration/test_conflict_comparison_seeding.py — CREATE — §23 Task 9
File: backend/tests/integration/test_chat_conflict_detection.py — CREATE — §23 Task 12
File: backend/tests/integration/test_ask_conflict_surfacing.py — CREATE — §23 Task 13
File: backend/tests/integration/test_migrations.py — MODIFY — §23 Task 2
File: backend/tests/api/test_conflicts.py — CREATE — §23 Task 11
File: backend/tests/fixtures/conflict_fixtures.py — CREATE — §27.7
File: backend/tests/conftest.py — MODIFY — add conflicts/conflict_statements to _CLEANUP_TABLES
```

### Frontend

```
File: frontend/src/lib/api/conflicts.ts — CREATE — §23 Task 14
File: frontend/src/hooks/queries/useConflicts.ts — CREATE — §23 Task 14
File: frontend/src/features/conflicts/index.ts — CREATE — §23 Task 15
File: frontend/src/features/conflicts/ConflictsPage.tsx — CREATE — §23 Task 15
File: frontend/src/features/conflicts/ConflictDetailPage.tsx — CREATE — §23 Task 15, 16
File: frontend/src/features/conflicts/ConflictCard.tsx — CREATE — §23 Task 15
File: frontend/src/features/conflicts/ConflictResolutionMenu.tsx — CREATE — §23 Task 15
File: frontend/src/features/conflicts/ConflictSeverityBadge.tsx — CREATE — §23 Task 15
File: frontend/src/features/conflicts/conflicts.css — CREATE — §23 Task 15
File: frontend/src/App.tsx — MODIFY — new routes + Dashboard KPI wiring — §23 Task 15
File: frontend/src/features/documents/TocPanel.tsx — MODIFY — conflict markers — §23 Task 16
File: frontend/src/features/compare/SectionNavigator.tsx — MODIFY (Phase-12-owned) — initialSection prop — §23 Task 16
File: frontend/src/features/ask/CitedAnswer.tsx — MODIFY — conflict_notice SSE handling — §23 Task 16
```

---

## 25. Data Flow

```
Background scan (nightly cron):
  org-scoped, CURRENT-version chunks only
     ↓ generate_candidates() — cross-document, similarity ≥ 0.83, top-5 per chunk
     ↓ check_contradiction() — constrained LLM call per candidate
     ↓ is_conflict=true AND confidence ≥ 0.6 ?
     ↓ persist_or_merge() — dedup by chunk identity (§14)
  INSERT conflicts + conflict_statements (per confirmed conflict, incremental,
  checkpointed per document)

Comparison-derived (triggered from inside Phase 12's handle_comparison):
  comparison_changes WHERE change_type=MODIFIED AND severity=MAJOR
     ↓ both sides classify_version_state == CURRENT ?
     ↓ persist_or_merge() — same dedup path as the background scan
  seed conflicts + conflict_statements

Chat CONFLICT_DETECTION:
  analyzer classifies intent → list_for_chat() (persisted, authorized OPEN
  conflicts only) → narrate_conflicts() (phrasing-only LLM call) → normal
  Message + Citations

Inline surfacing (every ask_stream call):
  retrieval → find_conflicts_among_chunks() (deterministic DB query) →
  conflict_notice SSE event (+ optional deterministic hint in generation
  context) → frontend ⚠ banner

Resolution:
  ConflictCard → resolve(decision, note) → OPEN → REVIEWED|DISMISSED
  (terminal) → AuditLogger.log(CONFLICT_RESOLVED)
```

---

## 26. Worker Flow

```
arq.cron (nightly) → for each active org with no in-flight CONFLICT_SCAN job:
    JobService.create_for_org_scan() → processing_jobs row (PENDING,
    document_version_id=NULL, checkpoint=NULL) → enqueue_after_commit(QUEUE_LOW)

run_processing_job() dispatch:
    job.job_type == CONFLICT_SCAN → _run_conflict_scan_job()
        → transition PROCESSING
        → ConflictService.run_scan(org_id, job, db)
              for each document (resumed from checkpoint.cursor_document_id):
                  candidates → contradiction check → persist_or_merge
                  commit + update checkpoint after each document
        → success: COMPLETED, checkpoint retains final counters
        → DeterministicJobError: FAILED immediately
        → transient error: existing _handle_failure retry/backoff (RETRYING
          → re-enqueued with backoff → resumes from last-committed checkpoint)
        → retries exhausted: FAILED + dead-letter (existing mechanism, unmodified)
```

---

## 27. Testing Strategy

### 27.1 Unit Tests

- `classify_conflict_severity` — every branch (critical section, statement_count≥3 × confidence thresholds, statement_count=2 × confidence thresholds).
- `is_critical_section` extraction — confirms Phase 12's existing `test_comparison_rules.py` still passes unmodified.
- `parse_contradiction_check` — valid, `is_conflict=false`, malformed JSON, out-of-range confidence clamping, missing fields.
- Conflict resolution state machine — `OPEN→REVIEWED`, `OPEN→DISMISSED`, rejecting a transition from a non-`OPEN` state.

### 27.2 Integration Tests

- Candidate generation: same-org only; different-org excluded; same-document excluded; `SUPERSEDED`/`SCHEDULED` chunks excluded from the pool; each unordered pair generated once.
- Dedup/merge: exact-pair skip across all three statuses; single-chunk growth restricted to `OPEN`; two-distinct-open-conflicts collision skip; concurrent-insert race → catch-and-refetch, no duplicate row.
- Comparison-derived seeding: qualifying change seeds; non-`MAJOR` change does not; non-`CURRENT` side does not; no duplicate on comparison re-fetch.
- Scan pipeline: full run on a seeded fixture; crash-and-resume with no duplicate conflicts; retry-exhaustion → `FAILED`.
- Chat `CONFLICT_DETECTION`: narrated, citation-backed answer for an authorized conflict; zero-conflict deterministic response; an inaccessible-document-backed conflict never appears in the narration.
- Inline surfacing: `conflict_notice` emitted only when ≥2 retrieved chunks share an `OPEN` conflict; never emitted for `REVIEWED`/`DISMISSED`.

### 27.3 API Tests

`POST /conflicts/{id}/resolve` → `200` valid decision / `409` already-resolved / `403` missing permission / `422` invalid decision value / `404` cross-org or unauthorized-source. `GET /conflicts` → status/severity filtering; private-document exclusion. `GET /conflicts/{id}` → `404` for any statement whose source is unauthorized. `GET /conflicts/scan-status` → correct shape, `unscanned_document_count` correctness.

### 27.4 Authorization Tests (explicit matrix, §28)

| Scenario | Expected |
|---|---|
| User can access every statement's source document | Conflict is listed/returned normally |
| User cannot access one statement's source document (private, not owner) | Conflict is absent from `GET /conflicts`; `GET /conflicts/{id}` → `404` |
| User cannot access any statement's source document | Same as above |
| User authorized at list time, one source's access_level changes to `private` (not owner) before a later `GET /conflicts/{id}` | Re-checked live — now `404` |
| Cross-tenant conflict ID | `404`, never `403` (no existence leakage) |
| `POST /resolve` without `conflict:resolve` | `403`, regardless of source-document access |
| `POST /resolve` with `conflict:resolve` but unauthorized source | `404` (source check takes precedence — never reveal the conflict exists to leak a "you'd be allowed to resolve it if only you could see it" signal) |

### 27.5 Worker Tests

Covered inside `test_conflict_scan_pipeline.py` (matching Phase 12's precedent of testing worker behavior alongside its pipeline, not in a separate `test_workers.py`): successful full scan; partial failure + resume; retry/backoff; idempotent re-scan (no duplicate conflicts); two concurrent scan-trigger attempts for the same org (only one `PENDING`/`PROCESSING` job exists at a time).

### 27.6 Chat/RAG Tests

Covered in §27.2's chat and inline-surfacing rows above; explicitly also test the negative case — a question whose retrieval returns chunks with **no** recorded conflict produces a completely normal answer with no `conflict_notice` event and no unexpected system-note in the generation context.

### 27.7 End-to-End Fixture

**New fixture, `backend/tests/fixtures/conflict_fixtures.py`**, alongside Phase 12's `comparison_fixtures.py`: two documents, both with a single `CURRENT`, `READY` version, embedded via `StubEmbeddingProvider`:
- **"HR Policy A"** — section "Vacation Approval": *"Vacation requests must be approved by the employee's direct manager."*
- **"HR Policy B"** — section "Leave Procedures": *"All vacation requests require HR department approval before submission."*

**Test** (`test_conflict_scan_pipeline.py`, the primary Phase 13 acceptance test), using a `StubLLMProvider` configured to return `{"is_conflict": true, "confidence": 0.9, "reason": "...", "conflict_topic": "Vacation Approval Authority"}` for this specific pair:
1. Run `ConflictService.run_scan` for the org.
2. Assert exactly one `Conflict` row created, `detection_method=BACKGROUND_SCAN`, `severity` computed via `classify_conflict_severity`.
3. Assert exactly two `ConflictStatement` rows, each with a real `chunk_id`/`document_version_id`/`page_id`.
4. Re-run the scan — assert no duplicate conflict created (row count unchanged).
5. `GET /conflicts` (as an Editor) → the conflict appears, `priority=ACTIVE` (both versions `CURRENT`).
6. `POST /conflicts/{id}/resolve {decision: "REVIEWED", note: "..."}` → `200`, `status=REVIEWED`; a `CONFLICT_RESOLVED` audit row exists.
7. Re-run the scan again — assert the resolved conflict is **not** reopened and no duplicate is created.
8. A chat message "Are there conflicting rules about vacation?" (scoped to both documents) → narrated answer referencing the conflict, citations on both statements.
9. (Manual/frontend verification, not automated) — the conflict is visible in `/app/conflicts`, reviewable/dismissible, and the TOC marker appears on both documents' relevant sections.

A second fixture variant adds a **superseded** third version of "HR Policy A" carrying an even older, further-contradicting statement, effective before the current version — asserting it is excluded from candidate generation entirely (§12) and never produces a phantom conflict.

---

## 28. Security Considerations

- **Tenant isolation:** every `ConflictService`/`ConflictRepository` method requires `organization_id` explicitly (via `TenantScopedRepository`); `semantic_search`'s existing mandatory `organization_id` predicate is a second, independent enforcement layer for candidate generation specifically.
- **Source authorization, no existence leakage:** a conflict is never returned (list, detail, or chat narration) unless every statement's source document passes the same access-level rule used everywhere else in this codebase. Cross-org and unauthorized-source both resolve to `404` — structurally identical, never `403` (§18, §27.4).
- **Chat protection:** `list_for_chat` filters to the conversation's already-resolved `accessible_document_ids` before any conflict reaches the narration LLM call — an inaccessible conflict is never even passed to the model, let alone mentioned in output.
- **Inline-surfacing safety:** §20 establishes why no additional authorization check is needed there — the conflict is only ever built from chunks the user's own already-permission-scoped retrieval returned.
- **Resolution authorization:** `conflict:resolve` (Admin/Editor only, seeded via migration, never inferred from `document:update` or any other existing key) is re-checked live against the database on every resolve call (`AuthorizationService.check_permission`), not just JWT claims.
- **No LLM-originated database writes:** the contradiction-check and narration LLM calls never call any persistence method directly; every write goes through `ConflictService`'s deterministic methods, which independently validate confidence thresholds, version state, and dedup identity before any `INSERT`.
- **Audit completeness:** every resolution is audited with actor, organization, conflict ID, decision, and timestamp — no separate audit table, reusing the existing `AuditLogger`.

---

## 29. Performance and Cost Considerations

- **Candidate volume bound:** scan corpus is CURRENT-version-only (excludes all historical versions by construction), org-scoped, and each chunk queries only its top-5 nearest cross-document neighbors above a 0.83 similarity floor — not full pairwise comparison. For a corpus of D documents averaging C chunks each, worst-case LLM calls per scan ≈ `D × C × 5` (before the similarity floor prunes most candidates in practice), a small constant factor over the corpus size, not quadratic.
- **LLM call cost:** the contradiction-check call is the same class of "fast, cheap, structured, `max_tokens` small, `temperature=0.0`" call as the query analyzer — not a full generation call. Bounded further by the 1,000-token-per-statement truncation cap.
- **Database writes:** incremental, per-document commits (not batched to end-of-scan) — bounded working-set, resumable, no risk of one enormous transaction.
- **Retry limits:** `JobType.CONFLICT_SCAN`'s existing retry policy (`2` attempts, already in `state_machines.py`) bounds worst-case re-execution cost; exponential backoff (existing mechanism) spaces retries out.
- **Comparison-derived seeding cost:** effectively free — one additional read query (`comparison_changes` by `comparison_id`) plus at most a handful of `persist_or_merge` calls per comparison; no LLM call at all for this trigger (§13).
- **Inline-surfacing cost:** one lightweight, indexed SQL query (`conflict_statements.chunk_id = ANY(...)`, using the new `ix_conflict_statements_chunk_id` index) per `ask_stream` call — negligible relative to the retrieval/generation calls already happening in that request.
- **V1 safeguards explicitly in place:** current-version-only scanning; org scoping; a fixed similarity threshold; a fixed top-K per chunk; nightly (not continuous) cadence; incremental persistence; deduplication before every write; bounded retries; provider timeout reuse from the analyzer's existing `_FAST_TIMEOUT_SECONDS`; a token cap per contradiction-check call.
- **Explicitly not built (premature optimization avoided):** no separate vector index, no dedicated scan-scheduling service, no per-org configurable concurrency — a single global nightly cadence is sufficient for the V1 vertical slice.

---

## 30. Edge Cases

| Edge case | Expected behavior |
|---|---|
| Two documents make compatible (non-contradictory) statements | Candidate generated (similarity may be high — related topics often read similarly), contradiction check returns `is_conflict=false` → not persisted. |
| Two documents make genuinely contradictory statements | Persisted, `detection_method=BACKGROUND_SCAN`, `priority=ACTIVE`. |
| Three documents disagree on the same point | First pair creates a 2-statement conflict; the third document's chunk, when later evaluated against either existing statement's chunk, triggers `persist_or_merge`'s single-chunk-growth path (§14) → the conflict grows to 3 statements, no duplicate conflict. |
| One document has multiple contradictory sections (each independently conflicting with a different other document) | Each section's chunk is evaluated independently; two unrelated conflicts are created (no shared chunk, so no merge) — correct, since they are genuinely unrelated topics. |
| Same document contradicts itself (two sections of the same document) | Candidate generation excludes same-document pairs entirely (§10, `exclude_document_id`) — never evaluated, never flagged. Intra-document consistency is out of scope for Phase 13. |
| Current vs. superseded document | Superseded side's chunks never enter the candidate pool (§12); a conflict already recorded when both sides were current shows `priority=LIKELY_RESOLVED` once one side becomes superseded, computed live. |
| Current vs. scheduled document | Scheduled side excluded from candidate pool entirely — no candidate generated. |
| Two currently-effective documents | The primary "genuine conflict" case — `priority=ACTIVE`. |
| Expired document | `classify_version_state` (Phase 12) already treats an expired version as non-`CURRENT` — same handling as superseded. |
| Missing effective dates on either side | `resolve_current_version`/`classify_version_state` already handle `NULL` dates correctly (Phase 12 §8.4 rule 2) — no Phase 13 special-casing needed. |
| Duplicate conflict candidates within one scan run | Pair-ordering rule (`chunk_b.id > chunk_a.id`) ensures each unordered pair is generated at most once (§10). |
| Same conflict detected by both background scan and comparison-derived seeding | `find_by_exact_pair` catches it regardless of which trigger runs first — the second trigger's `persist_or_merge` call is a no-op (§14). |
| Background scan repeats (nightly) | Fully idempotent — `find_by_exact_pair` prevents re-creation of any already-known (any status) pair. |
| Comparison-derived conflict + later background-scan discovery of a third disagreeing document | Single-chunk growth path adds the third statement to the comparison-derived conflict — `detection_method` on the `conflicts` row stays as originally recorded (first-detected wins; not updated on growth, since it reflects *how the conflict was first found*, not every contributing trigger). |
| Conflict already `REVIEWED` | New identical-pair candidates are skipped (`find_by_exact_pair` matches regardless of status); a genuinely new third-party disagreement creates a **new**, separate conflict rather than reopening the resolved one (§14). |
| Conflict already `DISMISSED` | Identical handling to `REVIEWED` — dismissal is equally terminal (§16). |
| Conflict source document deleted | No document-hard-delete flow exists in this codebase (verified, same finding as Phase 12) — `RESTRICT` FKs make this scenario structurally impossible today; flagged for whichever future phase adds hard deletion to explicitly handle existing conflicts. |
| Conflict source version/chunk deleted | Same as above — `RESTRICT`, not `SET NULL`/`CASCADE` (§9.2), by design, since Backend §46 rule 15 requires a real `chunk_id` always. |
| Unauthorized source | Conflict excluded from every read surface (§18, §27.4). |
| Cross-tenant source | Structurally impossible — candidate generation, dedup lookups, and every repository method are org-scoped (§28). |
| Provider timeout during contradiction check | `parse_contradiction_check`-adjacent call returns `None` → candidate skipped, scan continues (§11). |
| Provider malformed response | Same as above. |
| Worker crash mid-scan | Resumes from `checkpoint.cursor_document_id`'s next document (§15) — no duplicate conflicts on resume (dedup is idempotent regardless of re-processing). |
| Partial scan (job still `PROCESSING`) | `GET /conflicts` returns whatever has been persisted so far — conflicts are visible incrementally, never withheld until the whole scan finishes (matches Phase 12's "partial results visible during PROCESSING" precedent). |
| Concurrent scans for the same org | Prevented at the trigger level — the nightly cron checks for an in-flight job before creating a new one (§15); if one somehow still starts concurrently, `persist_or_merge`'s `IntegrityError` catch-and-refetch (§14) keeps the outcome correct regardless. |
| Very large corpus | Bounded per §29 — current-version-only, top-5-per-chunk, threshold-gated; no unbounded pairwise cost. |
| Very similar but non-conflicting documents (e.g., two near-duplicate templates) | High similarity generates a candidate; the contradiction check (which explicitly asks "contradictory... not merely related or compatible," §11) should return `is_conflict=false` — this is precisely why the LLM step exists rather than treating high similarity itself as a conflict signal. |
| Numeric changes ("5 days" vs. "7 days") | The canonical Phase 13 case — handled by both triggers. |
| Temporal/deadline changes | Same handling as numeric changes — no special-casing needed; the contradiction-check prompt explicitly includes "dates/deadlines" framing. |
| Different terminology expressing the same rule | Embedding similarity (§10) is meaning-based, not lexical, so paraphrased-but-compatible statements are still candidates; the contradiction check correctly returns `is_conflict=false` for these (a false-positive risk mitigated by the confidence threshold, §11). |
| Contradictions involving obligations/permissions/dates | All covered by the same general-purpose contradiction-check prompt (§11) — no per-category special logic, per the roadmap's own framing ("contradictory facts, requirements, rules, values, or obligations"). |

---

## 31. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Phase 12 not actually complete when Phase 13 work begins | Task 0's explicit verification gate (§23) — do not proceed past it until every check passes. |
| False-positive floods eroding user trust | Conservative thresholds (0.83 similarity, 0.6 confidence) chosen deliberately low-volume; roadmap's own designed mitigation (dismissal-ratio tuning) is explicitly out of scope for V1 but the raw `status` data needed for it already exists by construction. |
| Scan cost on large corpora | Bounded per §29; nightly cadence, not continuous; per-org scan windows explicitly deferred as a production enhancement, not required. |
| Contradiction-vs-related confusion on nuanced text | Constrained prompt explicitly offers the "merely related/compatible" alternative (§11) — the same design pattern the roadmap itself specifies. |
| `severity == MAJOR` as the comparison-derived qualification proxy imperfectly matching "factual/numeric" | Explicitly documented as a Plan-authored approximation (§13) — conservative (MAJOR is Phase 12's strictest tier), and avoids building a second, duplicate factual-content classifier. Revisit if false-negative seeding becomes a measured problem (Phase 19 territory). |
| `processing_jobs` schema now serving both single-version and org-wide job shapes | Mirrors Phase 12's own accepted-risk precedent for `comparison_id` — acceptable for V1; do not further generalize preemptively for hypothetical future job shapes (§9.3, §4.6 Gap 2). |
| Merging two independently-discovered conflicts about the same underlying fact (not implemented) | Explicitly scoped out (§5.2, §14) — logged and skipped, not silently mishandled; a genuine, documented V1 limitation rather than an unnoticed gap. |
| Frontend `frontend/src/lib` provenance still unresolved (Phase 12's own open question) | Not re-litigated here — Phase 12's fallback (reconstruct from call-site inference) applies transitively; flag again in this phase's implementation notes if still unresolved. |

---

## 32. Implementation Order

```
Step 0  — Verify Phase 12 is complete (§23 Task 0). Do not proceed otherwise.

Step 1  — Domain layer (Task 1): conflict_rules.py, is_critical_section
          extraction from comparison_rules.py.
          [Depends on: Step 0.]

Step 2  — Database migration (Task 2): conflicts, conflict_statements,
          processing_jobs extension, permission/role seeding, models.
          [Depends on: Step 0. Independent of Step 1's code, but written after
          it so column names match what Step 1's constants assume.]

Step 3  — semantic_search() extension (Task 3).
          [No dependency on Steps 1-2 — can happen in parallel.]

Step 4  — ConflictRepository (Task 4).
          [Depends on: Step 2.]

Step 5  — Candidate generation (Task 5).
          [Depends on: Steps 3, 4.]

Step 6  — Contradiction verification + parsing (Task 6).
          [Depends on: Step 5. Prompts (part of this task) have no
          dependency and can be written any time.]

Step 7  — Persistence, dedup/merge (Task 7).
          [Depends on: Steps 4, 6. This is the load-bearing correctness
          piece — budget real test time here.]

Step 8  — Background scan worker + cron (Task 8).
          [Depends on: Steps 5, 6, 7.]

Step 9  — Comparison-derived seeding hook (Task 9).
          [Depends on: Step 7, and Phase 12's handle_comparison existing
          (Step 0). Can run in parallel with Step 8.]

Step 10 — Resolution workflow + audit (Task 10).
          [Depends on: Step 4, Step 2's permission seeding.]

Step 11 — API layer (Task 11).
          [Depends on: Steps 7, 10.]

Step 12 — CONFLICT_DETECTION chat routing (Task 12).
          [Depends on: Step 4, Step 0 (Phase 12's intent-branch location).]

Step 13 — Inline RAG surfacing (Task 13).
          [Depends on: Step 4. Independent of Step 12 — can run in parallel.]

Step 14 — Backend tests: interleave with Steps 1-13 (each layer's tests
          written alongside that layer, per this codebase's established
          convention), not deferred to the end.

Step 15 — Frontend API client + hooks (Task 14).
          [Depends on: Phase 12 Task 13 (lib/ exists), Step 11 (real
          contract). Can start once Step 11's schemas are fixed, even before
          Step 11's handlers are fully implemented.]

Step 16 — Frontend Conflicts view (Task 15).
          [Depends on: Step 15.]

Step 17 — Frontend TOC markers, Compare Sources, inline chat notice (Task 16).
          [Depends on: Step 16, Phase 12's Comparison UI (Step 0), Step 13
          (conflict_notice event shape).]

Step 18 — End-to-end validation (§27.7 fixture + manual browser verification
          per Task 15/16's acceptance criteria).
          [Depends on: everything above.]
```

---

## 33. Definition of Done

Phase 13 is **not** done merely because a scan populates `conflicts`. It is done when:

- [ ] **Task 0's Phase 12 verification gate passes** — this plan's every other checkbox assumes it.
- [ ] A background scan against a corpus containing a genuine contradiction (the seeded fixture, §27.7) produces exactly one `Conflict` with correct topic/severity and exactly the right `ConflictStatement` rows, each with a real, resolvable `chunk_id`.
- [ ] A conflict can grow to more than two statements when a third disagreeing document is discovered (N-statement model, not a hardcoded pair).
- [ ] A superseded-version pseudo-conflict is recorded but its `priority` is computed as `LIKELY_RESOLVED`, never `ACTIVE` — and a genuinely scheduled/superseded version never even enters the candidate pool in the first place.
- [ ] Re-running the scan (or seeding via comparison, or both) never creates a duplicate `conflicts` row for the same statement pair, in any status.
- [ ] A qualifying Phase 12 comparison result (`MODIFIED`, `MAJOR`, both sides `CURRENT`) seeds a conflict without a second LLM call.
- [ ] `GET /conflicts`/`GET /conflicts/{id}` never expose a conflict backed by a source document the requesting user cannot access — verified by the full authorization matrix (§27.4).
- [ ] `POST /conflicts/{id}/resolve` correctly transitions `OPEN → REVIEWED`/`DISMISSED` (terminal, never reopened), is role-gated to Admin/Editor, and produces a `CONFLICT_RESOLVED` audit row.
- [ ] `CONFLICT_DETECTION` chat questions route to `ConflictService`, resolve to persisted, authorized conflicts, and produce a citation-backed narrated answer — never a fabricated one.
- [ ] A chat answer whose retrieved sources disagree on the same point surfaces a deterministic `conflict_notice`, never a hallucinated one.
- [ ] The frontend Conflicts view, `ConflictCard`, resolution menu, TOC `[•]` markers, and "Compare Sources" (reusing Phase 12's comparison view, section-anchored) are all functional end-to-end, verified manually.
- [ ] All tests in §27 pass, including the §27.7 end-to-end fixture.

---

## 34. Phase 13 Acceptance Criteria

(Restating §33 as a compact, checkable release-gate list — identical substance, terser form.)

1. Phase 12's Definition of Done is verified met (Task 0).
2. `alembic upgrade head` reaches revision `013` cleanly; `downgrade -1` reverses cleanly.
3. The seeded HR-policy fixture produces exactly one conflict with two correct statements and correct severity.
4. The conflict grows to N≥3 statements when a third disagreeing chunk is discovered, without creating a separate conflict.
5. A superseded-version candidate is excluded from the candidate pool; a conflict whose side later becomes superseded reports `priority=LIKELY_RESOLVED` at read time.
6. Re-scanning never duplicates any conflict, in any status.
7. A qualifying comparison result seeds a conflict deterministically, without a redundant LLM call.
8. The full authorization matrix (§27.4) passes — no cross-tenant or unauthorized-source leakage, ever a `404`, never a `403` for existence.
9. Resolution is role-gated (`conflict:resolve`, Admin/Editor), terminal, and audited (`CONFLICT_RESOLVED`).
10. `CONFLICT_DETECTION` chat questions produce citation-backed, non-fabricated answers grounded in persisted conflicts only.
11. Inline `conflict_notice` surfacing appears exactly when retrieved sources share an `OPEN` conflict, never otherwise.
12. Frontend Conflicts view, TOC markers, resolution workflow, and Compare Sources (via Phase 12's comparison UI, section-anchored) are all manually verified functional.
13. All new backend tests (§27) pass; the existing test suite, including Phase 12's own, remains green.

---

## 35. GLM 5.3 Flash Implementation Guidance

- **Do not start on this document's Task 1.** Run Task 0 first. If Phase 12 is not actually complete in the repository you are working in, stop and complete `PHASE-12-IMPLEMENTATION-PLAN.md` first — every table, function, and UI page this document references by name must actually exist before Phase 13 code can compile or pass its tests.
- **Do not re-implement anything Phase 12 already builds.** If you find yourself writing a second section-alignment function, a second version-state classifier, a second citation/provenance table, or a second comparison diff UI "just for conflicts," stop — you have misread this plan. Every one of those is reused by direct function/table reference, never duplicated.
- **`detection_method` values are `BACKGROUND_SCAN` / `COMPARISON_DERIVED` / `RETRIEVAL_TIME`** (uppercase) — not the lowercase values the roadmap/Backend doc's prose literally shows (§4.6 Gap 1). This is a deliberate, documented deviation for internal consistency with every other enum in this codebase.
- **Every threshold in this document (0.83 similarity, 0.6 confidence, the severity rule's 0.75/0.85 breakpoints, 5 candidates per chunk, 1,000-token truncation) is this plan's own proposal**, not an extracted requirement from any source document — implement them as named constants in `domain/conflict_rules.py` so they are trivially discoverable and tunable later, exactly as Phase 12 did for its own thresholds.
- **The LLM never writes to the database, never decides severity, never decides effective-date validity, never decides deduplication, and never decides the resolution lifecycle.** Every one of those is a deterministic function or repository method. If your implementation has any path where an LLM response directly triggers a `conflicts`/`conflict_statements` `INSERT`/`UPDATE` without passing through `ConflictService`'s deterministic gates first, that is a bug relative to this plan.
- **A conflict's identity is chunk-membership, never topic-text similarity.** Do not implement any fuzzy/semantic matching to decide whether two candidate pairs "are about the same conflict" — the dedup/merge algorithm in §14 is fully specified and chunk-identity-based; follow it exactly.
- **Authorization is source-document-based and checked on every read**, not just at detection time — a conflict that exists is not automatically visible to everyone who can query the API. The one deliberate exception is inline retrieval-time surfacing (§20), where reuse of retrieval's own upstream authorization is explicitly sufficient — do not add a redundant check there, and do not skip the check everywhere else by mistaking that one exception for a general rule.
- **Do not expand scope.** If you find yourself building conflict-graph merging across independently-discovered conflicts, a full Dashboard/Analytics rebuild, a new scheduler, a new queue system, per-org configurable scan windows, or `SUMMARY`/`EXTRACTION` intent routing, stop — none of these are Phase 13 (§5.2's explicit exclusions).
- **When in doubt about an existing convention** (file naming, schema naming, response shape, test organization), grep the nearest analogous Phase 12 artifact (e.g., how `ComparisonService`/`document_comparison_repository.py` is structured for `ConflictService`/`conflict_repository.py`) and match it exactly rather than introducing a new pattern.
