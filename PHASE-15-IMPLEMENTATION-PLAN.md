# Sprint 3 — Phase 15: Frontend Integration
## AI Document Intelligence Platform — Implementation-Ready Plan for GLM

**Prepared from:** Actual codebase inspection (September 2026) + Roadmap + Backend-Architecture-Documentation + Database-Architecture-Design-Documentation + Frontend-Design-Documentation
**Audience:** GLM implementing Phase 15
**Status:** Implementation-ready; do not modify architecture or add AI/RAG capabilities

---

## Table of Contents

1. [Phase 15 Overview](#1-phase-15-overview)
2. [Current Implementation Assessment](#2-current-implementation-assessment)
3. [Dependencies](#3-dependencies)
4. [Database Work](#4-database-work)
5. [Backend Work](#5-backend-work)
6. [Frontend Work](#6-frontend-work)
7. [End-to-End Integration Flows](#7-end-to-end-integration-flows)
8. [API Contract Verification](#8-api-contract-verification)
9. [Testing Plan](#9-testing-plan)
10. [Security Review](#10-security-review)
11. [Implementation Order](#11-implementation-order)
12. [Files to Create / Modify](#12-files-to-create--modify)
13. [Acceptance Criteria](#13-acceptance-criteria)
14. [Risks and Mitigations](#14-risks-and-mitigations)
15. [GLM Implementation Checklist](#15-glm-implementation-checklist)

---

## 1. Phase 15 Overview

### Objective

Consolidate the progressively-built frontend slices delivered in Phases 2–14 into the complete product experience: complete application shell, all documented screens, state-matrix compliance, responsive behavior, WCAG 2.1 AA accessibility, command palette, performance discipline, and Settings surfaces.

Phase 15 is a **consolidation phase, not a first frontend integration phase.** The backend API surface, frontend components, and React Query hooks already exist in meaningful form from prior phases. This plan corrects gaps, connects orphaned components, replaces placeholder pages, and applies cross-cutting quality bars.

### Scope

| Track | Included |
|---|---|
| Frontend | Complete app shell, Dashboard live wiring, Documents list page, Research Workspace (three-panel), Document Workspace (PDF.js upgrade, version switching, in-doc search), Search page, Compare/Conflict/Summary polish, Analytics expansion, Settings (all sections), Command palette, State-matrix audit, Responsive pass, Accessibility pass, Performance/code-splitting |
| Backend | Settings endpoints (profile PATCH, org PATCH, users list/role-change, audit-log read endpoint), any API contract corrections found during consolidation |
| Database | No new migrations required — verified below |
| AI/RAG | None — Phase 15 only consumes existing capabilities |

### Out of Scope

- New AI/RAG capabilities (Phases 9–14 own those)
- Security hardening adversarial suite (Phase 16)
- Full test pyramid consolidation (Phase 17)
- RAG evaluation framework (Phase 18)
- Observability platform (Phase 19)
- Production deployment (Phase 20)
- Token/cost analytics breakdown (Phase 19 feeds those endpoints)

### Expected Final Product State

After Phase 15 a seeded environment will support all seven documented user journeys (FE §7) using keyboard-only navigation, on desktop and tablet viewports, with every major screen showing correct loading/error/empty/success states. The `/app/documents` list page, all Settings sections, the three-panel Research Workspace, and the command palette will be functional for the first time. The Document Workspace will use PDF.js with a text layer and in-document search. All `PlaceholderPage` components will be eliminated.

---

## 2. Current Implementation Assessment

### 2.1 Verified Repository State

**Inspected files:**

- `frontend/src/App.tsx` — routing, PlaceholderPage usage
- `frontend/src/components/layout/AppShell.tsx` — sidebar, header
- All feature directories under `frontend/src/features/`
- All hook files under `frontend/src/hooks/queries/`
- All API client files under `frontend/src/lib/api/`
- `frontend/src/store/authStore.ts` — Zustand auth store
- `frontend/src/lib/realtime/sse.ts` — SSE transport
- `backend/app/main.py` — registered routers
- `backend/app/api/` — all 14 router files
- `backend/app/services/` — all 14 service files
- `backend/alembic/versions/` — 14 migration files (001–014)
- `frontend/package.json` — dependencies

### 2.2 Assessment Table

| Area | Current State | Gap | Phase 15 Action |
|---|---|---|---|
| **App Shell — Sidebar** | Static, no collapse, no persistence | Missing collapse toggle + localStorage persistence | Implement in `AppShell.tsx` + `uiStore.ts` |
| **App Shell — Header** | Shows user name + Sign out only | Missing: command bar, breadcrumbs, org switcher, user menu dropdown | Add `CommandPalette`, breadcrumb component, `UserMenu` dropdown |
| **App Shell — Processing Indicator** | Mounted and SSE-wired — **implemented** | None | Verify only |
| **Dashboard** | Conflicts KPI live; 3 KPIs show "—"; no recent widgets | Missing: 3 live KPIs, recent docs/questions widget, failed-jobs retry, new-org empty state | Wire missing KPIs; add Recent widgets |
| **Documents List Page** | `PlaceholderPage` — **missing** | Entire page missing | **New** `DocumentsPage.tsx` |
| **Document Upload** | No upload UI in frontend | **Missing** | `UploadDialog.tsx` using `POST /documents` |
| **Document Workspace — Viewer** | `<iframe src={signed_url}>` for PDF | PDF.js not used; no text layer; no zoom; no in-doc search | Replace iframe with PDF.js canvas + text layer |
| **Document Workspace — Version Switching** | Only shows current version | No version selector | Add version selector; wire `?version=N` URL param |
| **Document Workspace — TOC Scrollspy** | TOC renders but no scrollspy | Missing page-position sync | Implement scrollspy in `TocPanel.tsx` |
| **Research Workspace (Three-Panel)** | Does not exist — `AskPage` is two-panel | **Missing entirely** | Create `/app/research` route + `ResearchWorkspace.tsx` |
| **Search Page** | `PlaceholderPage` — **missing** | Entire page missing | **New** `SearchPage.tsx` |
| **Compare — Source Links** | `ChangeItem` shows plain text only | No "View source" navigation | Add "View in document" link using `old_chunk_id`/`new_chunk_id` |
| **Comparison — Polling Stop** | `useComparison` hook exists | Needs verification COMPLETED/FAILED stops polling | Verify `refetchInterval` returns false for terminal states |
| **Conflict Detail — Compare Sources** | Needs verification | Likely Phase 13 — confirm | Review `ConflictDetailPage.tsx` |
| **Summary — Citation Navigation** | `SummarySection.tsx` exists | Citation badges per bullet need verification | Review; add if missing |
| **Analytics Page** | 4 KPI cards only | Missing: range selector, processing metrics, quality bars | Expand `AnalyticsPage.tsx`; add `?days=` to backend |
| **Settings — ALL sections** | `PlaceholderPage` — **missing entirely** | Entire Settings feature missing | **New** `frontend/src/features/settings/` |
| **Command Palette** | Not implemented | **Missing entirely** | **New** `CommandPalette.tsx` |
| **State Matrix — Loading** | Inconsistent (some pages use `<div>Loading…</div>`) | No skeleton patterns on Dashboard/Documents | Standardize per FE §18 |
| **State Matrix — Permission-denied** | `AnalyticsPage.tsx` has it; others don't | Partial | Add `PermissionDenied` component on all pages |
| **Route Code Splitting** | All routes eager-imported in `App.tsx` | Missing | `React.lazy()` for all page components |
| **Zustand — Panel/UI State** | Only `authStore.ts` exists | Missing `uiStore.ts` + `panelStore.ts` | Add both stores |
| **Backend — Settings API** | No `/settings/*` router in `main.py` | **Missing — must be added** | Create `backend/app/api/settings.py` |
| **Backend — Audit Log Read** | Table exists; repository exists; no router | **Missing endpoint** | `GET /audit-logs` in `settings.py` |

---

## 3. Dependencies

### Phase 2–14 Dependencies (all exist)

| Phase | Deliverable Consumed in Phase 15 |
|---|---|
| Phase 2 | Auth (login, refresh, me, RBAC, `authStore`, `PrivateRoute`) |
| Phase 3 | `POST /documents`, `GET /documents`, `GET /documents/{id}`, collections |
| Phase 4 | `GET /documents/processing`, retry, SSE stream, `ProcessingIndicator` |
| Phase 5 | `GET /documents/{id}/pages`, `GET /documents/{id}/download` (signed URL) |
| Phase 6 | `GET /documents/{id}/toc`, `GET /documents/{id}/chunks` |
| Phase 7 | Embeddings in pgvector (consumed by search) |
| Phase 8 | `GET /search` with mode/filter params |
| Phase 9 | `POST /ask` (single-question, no conversation) |
| Phase 10 | Citations + `GET /documents/{id}/content` chunk content |
| Phase 11 | `POST /chat/conversations`, `GET /chat/conversations`, `GET /chat/conversations/{id}`, `POST /chat/messages/{id}/stop`, `POST /chat/messages/{id}/feedback`, SSE stream |
| Phase 12 | `POST /documents/compare`, `GET /documents/compare/{id}`, `GET /documents/compare/{id}/changes`, `GET /documents/compare/{id}/narration`, `GET /documents/{id}/versions` |
| Phase 13 | `GET /conflicts`, `GET /conflicts/{id}`, `POST /conflicts/{id}/resolve`, `GET /conflicts/scan-status` |
| Phase 14 | `GET /summaries/{docId}`, `POST /summaries/{docId}/regenerate`, `POST /extractions`, `GET /extractions/{id}`, `GET /analytics/summary` |

### Existing Frontend Slices (verified present in codebase)

- Auth pages: `LoginPage`, `RegisterPage`, `ForgotPasswordPage`, `ResetPasswordPage`
- `AppShell` with sidebar navigation
- `DocumentWorkspace` (iframe-based viewer + pages panel + TOC panel)
- `AskPage` (two-panel: conversations sidebar + chat area) with full SSE streaming
- `ComparisonPage` (version picker + comparison detail with polling)
- `ConflictsPage` + `ConflictDetailPage` + `ConflictCard` + `ConflictResolutionMenu`
- `SummaryPage` + `RegenerateButton` + `SummarySection` + `TopicTagList`
- `ExtractionListPage` + `ExtractionDetailPage`
- `AnalyticsPage` (4 KPI cards from `/analytics/summary`)
- `ProcessingStatusTracker` + `ProcessingStatusBadge` + `ProcessingIndicator`
- All React Query hooks: `useDocuments`, `useDocumentProcessing`, `useConversations`, `useComparisons`, `useConflicts`, `useSummary`, `useExtractions`, `useAnalytics`
- SSE transport (`consumeSseStream`)
- Typed API clients for all Phase 2–14 endpoints

### External Dependencies to Add

- **`pdfjs-dist`** — PDF.js rendering library; not yet installed; must be added to `package.json`
- Testing libraries (see §9.1) — not yet installed
- No other new npm packages strictly required for V1

---

## 4. Database Work

### Verdict: No new Phase 15 database migration required.

All 14 migration files (001–014) have been inspected. Every table required by Phase 15 features already exists:

| Phase 15 Surface | Table(s) Required | Migration | Status |
|---|---|---|---|
| Dashboard KPIs | `documents`, `conversations`, `messages`, `processing_jobs` | 004, 005, 010, 011 | ✅ Exist |
| Document Library | `documents`, `document_versions`, `collections` | 004 | ✅ Exist |
| Document Workspace | `document_pages`, `document_sections`, `document_chunks` | 006, 007 | ✅ Exist |
| Search | `document_chunks` (pgvector + FTS) | 007, 008, 009 | ✅ Exist |
| Research Workspace | `conversations`, `messages`, `citations` | 010, 011 | ✅ Exist |
| Compare | `document_comparisons`, `comparison_changes` | 012 | ✅ Exist |
| Conflicts | `conflicts`, `conflict_statements` | 013 | ✅ Exist |
| Summary | `document_summaries` | 014 | ✅ Exist |
| Extractions | `extraction_runs`, `extraction_items` | 014 | ✅ Exist |
| Analytics | `messages`, `citations`, `documents` (aggregates) | Various | ✅ Exist |
| Settings — Profile | `users` | 002 | ✅ Exist |
| Settings — Org | `organizations` | 002 | ✅ Exist |
| Settings — Users | `users`, `user_roles`, `roles` | 002 | ✅ Exist |
| Settings — Audit Log | `audit_logs` | 003 | ✅ Exist |

**Existing indexes supporting Phase 15 queries:**

- `idx_documents_org` — `documents.organization_id` — document list
- `idx_audit_logs_org_at` — `audit_logs(organization_id, created_at DESC)` — audit log page (primary read pattern)
- `idx_messages_conversation` — `messages.conversation_id`
- HNSW index on `document_chunks.embedding` — search
- GIN index on `document_chunks.fts_vector` — keyword search

No Phase 15 query patterns require new indexes.

---

## 5. Backend Work

Phase 15 backend work is limited to gaps discovered during actual codebase inspection. No business logic is redesigned. No services are restructured.

### 5.1 Settings and User Management Router (NEW)

**Gap:** No `/settings` or `/users` endpoints exist. The `users`, `roles`, `organizations`, `audit_logs` tables exist. `UserRepository` (in `user_repository.py`) and `AuditLogRepository` (in `refresh_token_repository.py`) exist. Only the router is missing.

**File:** `backend/app/api/settings.py` (NEW)

#### 5.1.1 PATCH /settings/profile

- **Permission:** None (own profile only)
- **Request body:** `{ "full_name": "string (optional)" }`
- **Response:** `UserMeResponse` (same shape as `GET /auth/me`)
- **Service call:** `UserRepository.update_profile(user_id, full_name)` — add this method if missing
- **Audit event:** None (profile name changes are not security-relevant per Backend §54)

#### 5.1.2 PATCH /settings/organization

- **Permission:** `org:manage` (admin only)
- **Request body:** `{ "name": "string (optional)" }`
- **Response:** `OrganizationResponse`
- **Note:** Do NOT expose `slug` as editable — slug changes break existing URLs and org-routing

#### 5.1.3 GET /settings/users

- **Permission:** `org:manage`
- **Query params:** `?limit=50&offset=0`
- **Response:** `{ "items": [{ "id", "email", "full_name", "roles": ["Admin"], "created_at", "is_active" }], "total", "limit", "offset" }`
- **Service call:** `UserRepository.list_by_org(organization_id, limit, offset)`

#### 5.1.4 PATCH /settings/users/{user_id}/roles

- **Permission:** `org:manage`
- **Request body:** `{ "role_ids": ["uuid", "uuid"] }`
- **Response:** Updated user object
- **Audit event:** `AuditAction.PERMISSION_CHANGED`
- **Guard:** Cannot remove own admin role (self-lockout prevention)

#### 5.1.5 GET /settings/roles

- **Permission:** `org:manage`
- **Response:** List of system roles + org custom roles with their permission keys

### 5.2 Audit Log Read Endpoint (NEW)

**Gap:** `audit_logs` table and `AuditLogRepository` exist but no read endpoint is registered.

**File:** `backend/app/api/settings.py` (same file, separate router or merged)
**Endpoint:** `GET /audit-logs`

- **Permission:** `org:manage` (audit logs contain PII-adjacent operational data)
- **Query params:** `?action=`, `?resource_type=`, `?user_id=`, `?from=ISO8601`, `?to=ISO8601`, `?limit=50`, `?offset=0`
- **Response:**
  ```json
  {
    "items": [{
      "id": "uuid",
      "organization_id": "uuid",
      "user_id": "uuid | null",
      "user_email": "string | null",
      "action": "DOCUMENT_UPLOADED",
      "resource_type": "document",
      "resource_id": "uuid | null",
      "metadata": {},
      "ip_address": "string | null",
      "created_at": "ISO8601"
    }],
    "total": 500,
    "limit": 50,
    "offset": 0
  }
  ```
- **Repository:** Add `AuditLogRepository.list_by_org(org_id, filters, limit, offset)` to `refresh_token_repository.py`
- **Note:** `user_email` must be joined from `users` table (single LEFT JOIN — do not embed emails in the stored `audit_logs` row)

### 5.3 Document List Response — Field Verification

**Gap:** The frontend `DocumentListItem` type (in `documents.ts:135`) expects a `processing_status` field. Verify `backend/app/schemas/document.py` `DocumentListResponse` returns this exact field name.

**Action:** Review and correct any field-name mismatch between `DocumentListResponse` and `DocumentListItem` without changing business logic.

### 5.4 Analytics Range Parameter (Minor Addition)

**Gap:** `GET /analytics/summary` aggregates over all time. Phase 15 adds a range selector (7d / 30d / 90d).

**File:** `backend/app/api/analytics.py`
**Change:** Add optional `?days: int = Query(None)` parameter. When provided, add `WHERE messages.created_at >= NOW() - INTERVAL ':days days'` to the relevant aggregation queries. Backward-compatible (omitting `days` returns all-time aggregate as before).

### 5.5 Comparison Response — Add Document IDs

**Gap:** `ComparisonResponse` contains `document_a_version_id` and `document_b_version_id` but not `document_a_id` / `document_b_id`. The frontend "View source" buttons in `ChangeItem` need the document IDs to build navigation URLs.

**File:** `backend/app/schemas/comparison.py`
**Change:** Add `document_a_id: UUID` and `document_b_id: UUID` to `ComparisonResponse`

**File:** `backend/app/api/compare.py`
**Change:** Populate the new fields by joining `document_versions` → `documents` when building the response

### 5.6 Search API — Frontend Client Only

**Gap:** `GET /search` exists in `backend/app/api/search.py`. No frontend `lib/api/search.ts` exists.

**Action:** Create `frontend/src/lib/api/search.ts` matching `backend/app/schemas/search.py` exactly. Do not modify the backend.

### 5.7 Router Registration

**File:** `backend/app/main.py`
Add after the analytics router:
```python
from app.api.settings import router as settings_router, audit_router
app.include_router(settings_router)
app.include_router(audit_router)
```

### 5.8 Layering Compliance

All backend work follows: **API router → Repository → Infrastructure**. No business logic enters the router layer. The settings endpoints are thin CRUD that may call `UserRepository` and `AuditLogRepository` directly. No new services needed; `require_permission()` from `app.api.deps` handles authorization.

---

## 6. Frontend Work

### 6.1 Application Shell

**Current state:** `AppShell.tsx` is a static sidebar with emoji icons + one sign-out button in the header.

#### 6.1.1 Sidebar Collapse

**New file:** `frontend/src/store/uiStore.ts`
```typescript
// State: sidebarCollapsed: boolean, commandPaletteOpen: boolean, documentViewMode: 'grid' | 'table'
// Actions: toggleSidebar(), openCommandPalette(), closeCommandPalette(), setDocumentViewMode()
// Persistence: zustand/middleware persist with localStorage
```

**File:** `frontend/src/components/layout/AppShell.tsx`
- Add collapse toggle button at the bottom of the sidebar (`aria-label="Toggle sidebar"`)
- When collapsed: show icons only (width collapses to 56px from `var(--sidebar-width)`)
- CSS: `.app-sidebar--collapsed` modifier; `transition: width var(--transition-base)`
- Keyboard shortcut: `[` when no input focused

#### 6.1.2 Header Expansion

**Changes to `AppShell.tsx`:**
- Replace flat user display with `UserMenu` dropdown (Profile link → Settings link → Sign out)
- Add `CommandPaletteButton` badge (`⌘K`) that triggers `uiStore.openCommandPalette()`
- Add `<Breadcrumbs />` component (see §6.1.3)
- Keep `ProcessingIndicator` at its current position — already wired correctly

#### 6.1.3 Breadcrumbs Component

**New file:** `frontend/src/components/layout/Breadcrumbs.tsx`
- Use `useMatches()` from react-router-dom v7
- Each route in `App.tsx` gets `handle: { crumb: 'Label' }` (can be a function receiving params for dynamic labels)
- Render `<nav aria-label="Breadcrumb"><ol>…</ol></nav>` when route depth > 1

#### 6.1.4 Route Code Splitting

**File:** `frontend/src/App.tsx`
```typescript
// Convert ALL page-level imports to React.lazy():
const DocumentsPage = React.lazy(() => import('./features/documents/DocumentsPage'))
const SearchPage = React.lazy(() => import('./features/search/SearchPage'))
// ... every route
```
Wrap `<Outlet />` in `AppShell.tsx` with `<React.Suspense fallback={<PageLoadingSpinner />}>`.

#### 6.1.5 404 Page

Replace `PlaceholderPage` in the `path="*"` route with `NotFoundPage` — title "Page not found", "Go to Dashboard" button.

---

### 6.2 Dashboard

**Current state:** Conflicts KPI is live. Three KPIs show "—". No recent widgets. No failed-jobs widget.

**Extract to:** `frontend/src/features/dashboard/DashboardPage.tsx`

#### 6.2.1 Wire All KPI Cards Live

| KPI | API | Hook |
|---|---|---|
| Total Documents | `GET /documents?limit=1` → `total` | `useDocumentList({ limit: 1 })` |
| Active Conversations | `GET /chat/conversations?limit=1` → `total` | `useConversations({ limit: 1 })` |
| Documents Processing | `GET /documents/processing` → `total` | `useProcessingJobs()` (exists) |
| Open Conflicts | `GET /conflicts?status=OPEN` → items.length | `useConflicts('OPEN')` (exists) |

Each KPI card: loading = animated skeleton pulse; error = "—" with retry icon; permission-denied = "N/A" with tooltip.

#### 6.2.2 Recent Documents Widget

- Hook: `useDocumentList({ limit: 5, sort: 'updated_at', order: 'desc' })`
- Renders: table rows — document name (link), type, status badge, relative timestamp
- States: skeleton (3 rows), empty ("No documents yet — upload your first"), error (retry link)

#### 6.2.3 Recent Questions Widget

- Hook: `useConversations({ limit: 5 })`
- Renders: list — conversation title (link to `/app/ask?conversation={id}`), relative date
- States: skeleton, empty ("No questions asked yet"), error

#### 6.2.4 Processing Activity Widget

- Hook: `useProcessingJobs()` (already exists)
- Renders: active jobs with document name, stage, progress bar; failed jobs get "Retry" button
- States: empty ("No documents currently processing"), error

#### 6.2.5 New Organization Empty State

When `documentsTotal === 0` AND `questionsTotal === 0`: replace the grid with a centered empty state card with "Upload your first document" CTA (opens `UploadDialog`).

---

### 6.3 Documents List Page (NEW)

**New file:** `frontend/src/features/documents/DocumentsPage.tsx`
**Route:** `/app/documents` (currently `PlaceholderPage`)

- Page header: title "Documents", filter bar, Upload button (permission-gated: `document:upload`)
- Grid/Table toggle (state in `uiStore.documentViewMode`)
- Document cards/rows: name, type, department, status badge (`ProcessingStatusBadge`), page count, updated_at, actions menu (View, Delete)
- States: Loading skeleton (6 cards/rows), empty with Upload CTA or "No documents match your filters", error with Retry

**New component:** `frontend/src/features/documents/UploadDialog.tsx`
- `<dialog>` element, `aria-modal="true"`, focus-trapped
- Drag-and-drop zone + file picker
- Metadata form: name, document_type, department, access_level
- Inline file type validation (PDF/DOCX only); file size limit check
- On submit: `POST /documents` multipart form
- On success: close dialog, invalidate `['documents', 'list']`, show "Document uploading" notification

**Hooks to add in `useDocuments.ts`:**
```typescript
export function useDocumentList(params: DocumentListParams)
export function useUploadDocument() // mutation
export function useDeleteDocument() // mutation
```
Also extract `useDocumentVersions` from `useComparisons.ts` into `useDocuments.ts`.

---

### 6.4 Document Workspace

**Current state:** `DocumentWorkspace.tsx` uses an `<iframe>` for PDF rendering; no version selector; no in-doc search; no TOC scrollspy.

#### 6.4.1 PDF.js Integration

Install: `pdfjs-dist`

Configure in `frontend/src/main.tsx`:
```typescript
import { GlobalWorkerOptions } from 'pdfjs-dist'
GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.mjs', import.meta.url).toString()
```

**New file:** `frontend/src/features/documents/PdfViewer.tsx`
- Props: `url: string`, `page: number`, `onPageChange(n: number)`, `onTextLayerReady()`
- Render one page at a time (memory-safe for large docs)
- Canvas layer for PDF rendering
- Text layer: absolutely-positioned spans over canvas for selection + screen-reader access
- Zoom: fit-width, fit-page, 50/75/100/125/150%
- Keyboard nav: Left/Right arrows, Page Up/Down
- In-doc search: uses `getTextContent()` to build a page-text index; highlights matches with `<mark>` overlay
- Citation deep-link: on mount, jump to `?page=N` then highlight `?q=text` in text layer

Replace the `<iframe>` section in `DocumentWorkspace.tsx` with `<PdfViewer>`.

#### 6.4.2 Version Selector

**File:** `DocumentWorkspace.tsx`
- Add version dropdown in workspace header using `useDocumentVersions(documentId)`
- On version change: update `?version=N` URL param; workspace reloads pages/TOC/download URL

#### 6.4.3 TOC Scrollspy

**File:** `TocPanel.tsx`
- Accept `currentPage: number` prop from the viewer
- When active page is within a section's `start_page`–`end_page` range, highlight that TOC item with `aria-current="true"` and the CSS active class
- On TOC item click: fire `onSectionClick(start_page)` callback → viewer jumps to that page

#### 6.4.4 In-Document Search

**New file:** `frontend/src/features/documents/DocumentSearchBar.tsx`
- Input field with match counter "2 / 7"
- Previous/Next buttons (keyboard: F3/Shift+F3)
- Intercept `Ctrl+F` / `⌘F` in the viewer container via `keydown` handler
- Results highlighted in PDF text layer using `<mark>` overlay spans
- On close: clear all highlights, return focus to viewer

#### 6.4.5 Add to `documents.ts`

```typescript
export function getDocumentContentApi(
  documentId: string,
  params: { chunk_id?: string; page?: number }
): Promise<DocumentContentResponse>
```
The endpoint `GET /documents/{id}/content` already exists in `backend/app/api/documents.py` (line 26).

#### 6.4.6 Permission-Denied State

When `getDocumentApi` returns 403: render `<PermissionDenied>` instead of the workspace. Do not render a generic error card.

---

### 6.5 Research Workspace (Three-Panel, NEW)

**New route:** `/app/research`

**New files:**
- `frontend/src/features/research/ResearchWorkspace.tsx`
- `frontend/src/features/research/EvidencePanel.tsx`
- `frontend/src/features/research/ResearchTranscript.tsx`
- `frontend/src/features/research/ScopeChips.tsx`
- `frontend/src/store/panelStore.ts`
- `frontend/src/features/research/research.css`

#### Layout (≥1280px)

| Panel | Default width | Content |
|---|---|---|
| Left | 280px, collapsible | Scope chips + Conversation history |
| Center | fluid | Question input + Chat transcript with citation badges |
| Right | 360px, collapsible | Evidence panel — source snippet for last-clicked citation |

Panels are resizable via drag handles. Widths and collapsed states persist in `panelStore` (Zustand + localStorage).

**Responsive:**
- `≥1280px`: three-panel layout
- `1024–1279px`: left panel collapses to slide-out drawer
- `<1024px`: redirect to `/app/ask`

#### Citation → Evidence Synchronization

On citation badge click in the transcript:
1. Call `panelStore.setActiveCitation(citation)` — no page navigation
2. `EvidencePanel` reads `activeCitation` from store
3. Calls `getDocumentContentApi(doc_id, { chunk_id })` → displays source snippet
4. Highlights `quoted_text` within the snippet

**panelStore shape:**
```typescript
interface PanelState {
  leftWidth: number     // default 280
  rightWidth: number    // default 360
  leftCollapsed: boolean
  rightCollapsed: boolean
  activeCitation: AskCitation | null
  setActiveCitation: (c: AskCitation | null) => void
  setPanelWidth: (panel: 'left' | 'right', width: number) => void
  togglePanel: (panel: 'left' | 'right') => void
}
```

---

### 6.6 Search Page (NEW)

**New files:**
- `frontend/src/features/search/SearchPage.tsx`
- `frontend/src/features/search/SearchResultCard.tsx`
- `frontend/src/lib/api/search.ts`
- `frontend/src/hooks/queries/useSearch.ts`
- `frontend/src/features/search/search.css`

#### `frontend/src/lib/api/search.ts`

```typescript
export type SearchMode = 'semantic' | 'keyword' | 'hybrid'

export interface SearchRequest {
  query: string
  mode?: SearchMode
  document_ids?: string[]
  collection_ids?: string[]
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
}

export interface SearchResultItem {
  chunk_id: string
  document_id: string
  document_name: string
  version_id: string
  page_number: number | null
  section_title: string | null
  content: string
  score: number
  highlighted_content?: string
}

export interface SearchResponse {
  query: string
  mode: SearchMode
  items: SearchResultItem[]
  total: number
}

export function searchDocuments(params: SearchRequest): Promise<SearchResponse>
```

#### `useSearch.ts`

```typescript
// Debounced hook — 300ms delay
// queryKey: ['search', params]
// enabled: params.query.length >= 2
// staleTime: 30_000
```

#### SearchPage Layout

- Mode toggle: Semantic / Keyword / Hybrid (`role="group"`)
- Search input (full-width, autofocused)
- Collapsible filter bar: date range, document multi-select, collection filter
- "Group by document" toggle
- Results: `SearchResultCard` — section title, snippet with highlighted terms, document name → page link
- Empty state (query present): "No results for '{query}'"
- Empty state (no query): "Type to search your knowledge base"
- Processing-exclusion notice: "N documents are still processing and excluded from results"
- Loading: 5 skeleton result cards
- Error: retry button

**Navigation on result click:** `/app/documents/{doc_id}?page={page_number}&q={snippet}`

---

### 6.7 Compare / Conflicts / Summary — Polish

#### 6.7.1 Comparison — Source Navigation from ChangeItem

**File:** `ComparisonPage.tsx` (or `ChangeItem` sub-component)

Add "View source" buttons when `old_chunk_id` or `new_chunk_id` is non-null:
- Old: `/app/documents/{document_a_id}?chunk={old_chunk_id}`
- New: `/app/documents/{document_b_id}?chunk={new_chunk_id}`

**Requires:** `document_a_id` and `document_b_id` in `ComparisonResponse` (backend §5.5).

#### 6.7.2 Comparison — Polling Stop Verification

**File:** `useComparisons.ts` — verify `refetchInterval` returns `false` for `COMPLETED` and `FAILED` status. Fix if polling continues past terminal states.

#### 6.7.3 Conflict Detail — "Compare Sources" Button

**File:** `ConflictDetailPage.tsx` — verify the button navigates to `/app/compare?versionA={statement_a.version_id}&versionB={statement_b.version_id}&section={section}`. This was delivered in Phase 13. If missing, add it using the conflict's `statements` array (first two statements).

#### 6.7.4 Summary — Citation Badges per Bullet

**File:** `SummarySection.tsx` — verify per-bullet citations render as `CitationBadge` components navigating to source pages. Inspect the backend `SummaryPayload` type for per-item citation data. If data is present in the database but absent from the payload, this is a Phase 15 backend contract correction — add citation data to `SummaryItem` in `backend/app/schemas/summary.py`.

---

### 6.8 Analytics Page Expansion

**File:** `frontend/src/features/analytics/AnalyticsPage.tsx`

Changes:
1. **Range selector:** Tab strip "7d / 30d / 90d / All time" → sends `?days=N` to `/analytics/summary` (requires backend §5.4)
2. **Quality section:** CSS progress bars for `groundedAnswerPct` and `citationCoveragePct` (no chart library)
3. **Per-widget independent error states:** Each KPI card catches and displays its own error without affecting others
4. **"View as table" toggle:** All data rendered as a `<table>` element for screen-reader accessibility (FE §6.14)
5. **Token/cost section:** Placeholder card "Cost analytics coming in a later phase" with a locked icon

The permission check (`analytics:read`) is already implemented — keep as-is.

---

### 6.9 Settings Feature (NEW)

**New directory:** `frontend/src/features/settings/`

**Route restructure in `App.tsx`:**
```tsx
<Route path="settings" element={<SettingsLayout />}>
  <Route index element={<Navigate to="profile" replace />} />
  <Route path="profile" element={<ProfilePage />} />
  <Route path="organization" element={<OrganizationPage />} />
  <Route path="users" element={<UsersPage />} />
  <Route path="roles" element={<RolesPage />} />
  <Route path="ai" element={<AISettingsPage />} />
  <Route path="documents" element={<DocumentSettingsPage />} />
  <Route path="integrations" element={<IntegrationsPage />} />
  <Route path="security" element={<SecurityPage />} />
  <Route path="audit-log" element={<AuditLogPage />} />
</Route>
```

**SettingsLayout:** Left nav with all 9 sections (icon + label). Users without `org:manage` see only Profile section; all others show `<PermissionDenied>`. On mobile: tab bar at top.

**ProfilePage:** Form with full_name (editable), email (read-only), org name (read-only). Save → `PATCH /settings/profile` → `authStore.initialize()` to refresh `currentUser`.

**OrganizationPage:** Form with org name (editable), slug (read-only). Save → `PATCH /settings/organization`. Requires `org:manage`.

**UsersPage:** Table — name, email, roles (badges), joined date, change-role action button → modal with role checkboxes. Pagination (50/page). Permission gate: `org:manage`.

**RolesPage:** Display system roles with their permission keys. Read-only view showing what each role can do.

**AISettingsPage / DocumentSettingsPage / IntegrationsPage / SecurityPage:** Documented placeholders with "Coming in Phase 16" messages for fields that require later implementation.

**AuditLogPage:** Filter bar (action type select, date range, user search) → paginated table (timestamp, user, action, resource, IP). Export button generates client-side CSV from the fetched page. Permission gate: `org:manage`.

**New API client:** `frontend/src/lib/api/settings.ts`
**New hooks:** `frontend/src/hooks/queries/useSettings.ts`

---

### 6.10 Command Palette (NEW)

**New files:**
- `frontend/src/components/CommandPalette.tsx`
- `frontend/src/components/CommandPalette.css`

**Trigger:** `⌘K` (Mac) / `Ctrl+K` (Windows) global listener attached in `AppShell`; also the header button.
**State:** `uiStore.commandPaletteOpen: boolean`

- `role="dialog"`, `aria-modal="true"`, `aria-label="Command palette"`, focus-trapped
- `Escape` closes; focus returns to trigger
- Search input autofocused on open
- Results sections:
  1. **Documents** — client-side fuzzy filter over `useDocumentList` data
  2. **Recent Conversations** — client-side fuzzy filter over `useConversations({ limit: 10 })` data
  3. **Quick Actions** — static list: Upload Document, New Conversation, Go to Search, Go to Analytics, Go to Settings
- Keyboard: `↑↓` navigates items; `Enter` activates; `Tab` cycles sections
- ARIA: `aria-activedescendant` tracks focused item; `aria-live="polite"` announces result count

---

### 6.11 State Matrix Audit

Systematic verification pass over every screen × every state (FE §18). Not feature development — a checklist-driven QA pass.

| Screen | Loading | Empty | Success | Error | Processing | Permission-denied | Partial |
|---|---|---|---|---|---|---|---|
| Dashboard | Add skeleton to missing KPIs | Add new-org state | ✅ | Add per-widget | ✅ (processing widget) | N/A | N/A |
| Documents | **Add** | **Add** | **New page** | **Add** | ✅ (status badge) | **Add** | N/A |
| Document Workspace | ✅ | ✅ (no pages) | ✅ | ✅ | ✅ (tracker) | **Add** | ✅ (partial OCR) |
| Ask AI | ✅ | ✅ | ✅ | ✅ (error turn) | ✅ (streaming) | N/A | ✅ (stopped answer) |
| Research Workspace | **New** | **New** | **New** | **New** | **New** | N/A | **New** |
| Search | **New** | **New** | **New** | **New** | N/A | N/A | N/A |
| Compare | ✅ | ✅ (no changes) | ✅ | ✅ | ✅ (PENDING/PROCESSING) | N/A | ✅ (degraded alignment) |
| Conflicts | ✅ | ✅ | ✅ | Add | N/A | ✅ (canResolve) | ✅ (unscanned banner) |
| Summary | ✅ (skeleton) | ✅ (per section) | ✅ | ✅ | ✅ (PENDING/regen) | N/A | ✅ (sampling) |
| Extractions | ✅ | ✅ | ✅ | Add | N/A | ✅ (canCreate) | N/A |
| Analytics | ✅ | N/A | ✅ | Add per-widget | N/A | ✅ | N/A |
| Settings | **New** | **New** | **New** | **New** | N/A | **New** | N/A |

**Shared component needed:** `frontend/src/components/PermissionDenied.tsx`

---

### 6.12 Responsive Design

Breakpoints from `index.css` (existing tokens):
- `--breakpoint-md: 768px`
- `--breakpoint-lg: 1024px`
- `--breakpoint-xl: 1280px`

| Surface | Mobile (<768px) | Tablet (768–1279px) | Desktop (≥1280px) |
|---|---|---|---|
| AppShell sidebar | Hidden; hamburger opens slide-out | Collapsible icon-only | Full labels |
| Documents list | Single-column cards | 2-column grid | Table view available |
| Research Workspace | Redirect to `/app/ask` | Two-panel (left as drawer) | Three-panel |
| Settings | Tab bar at top | Tab bar at top | Left-nav + content |
| Command Palette | Full-screen | Centered modal 600px | Centered modal 600px |
| Dashboard KPIs | Single-column | 2-column grid | 4-column grid |

---

### 6.13 Accessibility Pass

**Keyboard shortcuts (new, global — implemented via `keydown` handler in `AppShell`):**

| Action | Shortcut |
|---|---|
| Open command palette | `⌘K` / `Ctrl+K` |
| Close modal/palette | `Escape` |
| Navigate palette | `↑↓` arrows |
| Go to Dashboard | `G D` (when no input focused) |
| Go to Ask AI | `G A` |
| Go to Search | `G S` |
| Sidebar toggle | `[` |
| PDF prev/next page | `←` / `→` |
| PDF search prev/next match | `Shift+F3` / `F3` |

**ARIA requirements for new components:**
- All modals: `role="dialog"`, `aria-modal="true"`, `aria-labelledby` → title
- Tab bars: `role="tablist"` + `role="tab"` + `aria-selected`
- Loading regions: `aria-busy="true"`
- Streaming/live regions: `aria-live="polite"`
- Citation badges: `aria-label="Citation N: {document_name}, page {page}"`
- Progress bars: `role="progressbar"`, `aria-valuemin=0`, `aria-valuemax=100`, `aria-valuenow`
- PDF canvas: `role="img"`, `aria-label="Page {N} of {total}"` (text layer provides actual content)

**Focus management rules:**
- Modal open → focus moves to first focusable element
- Modal close → focus returns to trigger
- Page navigation → focus moves to `<h1>`
- Streaming starts → focus stays on Stop button

**Contrast:** Use only `index.css` token colors. Do not introduce custom colors in new components.

---

### 6.14 Frontend Performance

#### Route Code Splitting

Convert all page imports in `App.tsx` to `React.lazy()`. Highest-impact change: eliminates eager loading of all page code on first visit.

#### React Query Cache Invalidation Map

| Mutation | Invalidates |
|---|---|
| Upload document | `['documents', 'list']`, `['documents', 'processing']` |
| Delete document | `['documents', 'list']`, `['documents', documentId, 'detail']` |
| Retry processing | `['documents', documentId, 'status']`, `['documents', 'processing']` |
| New conversation | `['conversations', 'list']` |
| Resolve conflict | `['conflicts', conflictId]`, `['conflicts', 'list']` |
| Regenerate summary | `['summary', documentId]` |
| Update profile | Call `authStore.initialize()` to re-fetch `currentUser` |
| Update org | Call `authStore.initialize()` |

#### staleTime Discipline

| Query | staleTime |
|---|---|
| Document detail | 15 000 ms |
| Document list | 30 000 ms |
| Conversations list | 60 000 ms |
| Comparison detail | 0 (polling until COMPLETED) |
| Conflict list | 60 000 ms |
| Analytics summary | 300 000 ms |
| Audit logs | 60 000 ms |
| Processing status | 0 (SSE-driven; polling is fallback) |

#### SSE Cache Write Pattern

The existing pattern in `useDocumentProcessing.ts` (`queryClient.setQueryData()` on each SSE event) is correct. All new SSE consumers must follow the same pattern — never create a separate state variable for SSE-driven data.

---

## 7. End-to-End Integration Flows

### Flow 1: Login → Dashboard

**Path:** `LoginPage` → `authStore.login()` → `POST /auth/login` → `GET /auth/me` → `DashboardPage` (all 4 KPIs load in parallel)

**Failure cases:**
- 401 wrong credentials: error message on form
- Token expired on revisit: `initialize()` fails → `clearSession()` → `/login?reason=expired`

### Flow 2: Dashboard → Documents → Document Workspace (Journey 1)

**Path:** Recent-docs row link → `/app/documents/{id}` → `DocumentWorkspace` → PDF.js renders document

**API calls:** `GET /documents/{id}`, `GET /documents/{id}/status`, `GET /documents/{id}/download`, `GET /documents/{id}/pages`, `GET /documents/{id}/toc`

**States:** Workspace skeleton → metadata loads → viewer loading → PDF rendered → pages fill in → TOC loads

**Failure:** 403 → `<PermissionDenied>`; 404 → NotFoundCard

### Flow 3: Ask AI → Streaming Answer → Citation → Evidence (Journey 2 — Release Gate)

**Path:** `AskPage` → question submitted → SSE tokens stream → citation badges render → citation click → `/app/documents/{id}?page=N&q=text` → `DocumentWorkspace` → page N opens, text highlighted

**Must complete in ≤ 2 clicks from citation badge to exact source page.**

### Flow 4: Research Workspace — Citation Without Navigation (Journey 5)

**Path:** `/app/research` → question → answer with citation badges → citation badge click → `panelStore.setActiveCitation(c)` → `EvidencePanel` loads source snippet → right panel shows evidence **without navigating away**

### Flow 5: Search → Result → Document/Page/Highlight

**Path:** `SearchPage` → type query → debounce 300ms → `GET /search` → `SearchResultCard` → click → `/app/documents/{id}?page=N&q=snippet` → `DocumentWorkspace` opens at correct page with highlight

### Flow 6: Compare → Diff → Source Evidence (Journey 4)

**Path:** `ComparisonPage` → version picker → `POST /documents/compare` → polling → COMPLETED → change list → "View source" on a `ChangeItem` → `/app/documents/{doc_a_id}?chunk={old_chunk_id}`

### Flow 7: Conflicts → Resolution (Journey 6)

**Path:** `ConflictsPage` → `ConflictCard` → `ConflictDetailPage` → "Compare Sources" → `/app/compare?versionA=…&versionB=…` → return → `ConflictResolutionMenu` → `POST /conflicts/{id}/resolve` → conflict moves to "Reviewed" tab

### Flow 8: Document → Summary → Citation

**Path:** `DocumentWorkspace` → "Summary" button → `SummaryPage` → section bullets with `CitationBadge` → badge click → `/app/documents/{id}?page=N&q=text`

### Flow 9: Processing Indicator → Status → Document

**Path:** Header `ProcessingIndicator` badge → popover shows active jobs → document name link → `/app/documents/{id}` workspace

### Flow 10: Settings → Update Profile

**Path:** Header user menu → Settings → `ProfilePage` → edit name → Save → `PATCH /settings/profile` → success → `authStore.initialize()` refreshes `currentUser.full_name` in header

### Flow 11: Settings → Audit Log

**Path:** Settings left nav → Audit Log → `AuditLogPage` → filter by action type → `GET /audit-logs?action=DOCUMENT_UPLOADED` → filtered table

### Flow 12: Command Palette — Navigate to Document

**Path:** `⌘K` → palette opens → type "policy" → client-side filter shows "Policy 2026" → `Enter` → navigate to `/app/documents/{id}` → palette closes

---

## 8. API Contract Verification

Before building each Phase 15 consumer, verify the contract against the actual backend schema:

| Feature | Frontend Call | Backend | Gap | Required Change |
|---|---|---|---|---|
| Document list | `GET /documents` → `DocumentListItem.processing_status` | Verify `DocumentListResponse` field name | Possible name mismatch | Review `backend/app/schemas/document.py` |
| Comparison with doc IDs | `ComparisonResponse.document_a_id` | Not present — only version IDs | **Gap** | Add to `comparison.py` schema + `compare.py` router |
| Analytics range | `GET /analytics/summary?days=30` | No `days` param | **Gap** | Add to `analytics.py` |
| Settings profile PATCH | `PATCH /settings/profile` | **Missing** | **Gap** | Create `settings.py` |
| Settings org PATCH | `PATCH /settings/organization` | **Missing** | **Gap** | Same |
| Settings users GET | `GET /settings/users` | **Missing** | **Gap** | Same |
| Settings user roles | `PATCH /settings/users/{id}/roles` | **Missing** | **Gap** | Same |
| Settings roles GET | `GET /settings/roles` | **Missing** | **Gap** | Same |
| Audit log GET | `GET /audit-logs` | **Missing** | **Gap** | Same |
| Search API client | `GET /search` → `SearchResponse` | Backend exists; no frontend client | **Gap** | Create `search.ts` |
| Document content | `GET /documents/{id}/content` | Backend exists (`documents.py:26`) | No frontend function | Add `getDocumentContentApi` to `documents.ts` |
| Summary per-item citations | `SummaryPayload.items[].citations` | Inspect `summary.py` schema | Possible gap | Verify; add if missing |

---

## 9. Testing Plan

### 9.1 Frontend Testing Setup (Add to project)

**Install:**
```json
"vitest": "^1.x",
"@vitest/ui": "^1.x",
"@testing-library/react": "^14.x",
"@testing-library/user-event": "^14.x",
"@testing-library/jest-dom": "^6.x",
"jsdom": "^25.x"
```

**Add to `package.json` scripts:**
```json
"test": "vitest run",
"test:watch": "vitest"
```

**New file:** `frontend/vitest.config.ts`

### 9.2 Component Tests (New `*.test.tsx` files)

| Component | Test Coverage |
|---|---|
| `CommandPalette` | Opens on ⌘K; closes on Escape; keyboard navigation; Enter activates item |
| `UploadDialog` | Rejects non-PDF/DOCX; accepts PDF; submits multipart form; shows error |
| `ProcessingStatusBadge` | Correct label and CSS class for each `VersionStatus` |
| `ProcessingStatusTracker` | Loading state; READY state; FAILED state + retry button |
| `ConflictResolutionMenu` | Renders options; submit calls resolve; disabled when `canResolve=false` |
| `DocumentsPage` | Loading skeleton; empty state; populated list; error + retry |
| `AuditLogPage` | Filter changes query params; loading skeleton; empty state |
| `PermissionDenied` | Renders with message |
| `PdfViewer` | Renders page 1 of a test PDF; text layer present in DOM |

### 9.3 Backend Tests (New files)

**`backend/tests/api/test_settings.py`**
- All Settings endpoints with valid admin credentials → 200
- All admin-only endpoints with non-admin credentials → 403
- All endpoints with unauthenticated request → 401
- `PATCH /settings/users/{id}/roles` — self-lockout guard: cannot remove own admin role → 422
- `GET /settings/users` — pagination returns correct subset
- Org-isolation: user from org A cannot see org B's users (two-org fixture)

**`backend/tests/api/test_audit_logs.py`**
- `GET /audit-logs` returns only current org's events (two-org fixture with events in both)
- Action filter returns only matching events
- Date range filter returns only events within range
- Non-admin request → 403
- Pagination: `limit=5&offset=5` returns correct window

**Extend `backend/tests/api/test_analytics.py`**
- `GET /analytics/summary?days=30` returns only events from the last 30 days (seeded with events at -60 days and -15 days; only -15 day events appear)
- Without `?days` returns all events (backward compatibility)

### 9.4 End-to-End Tests (Playwright)

**Install:** `@playwright/test` (dev dependency)

**Priority flows (acceptance gates):**

1. **Journey 2 — Release Gate:** Login → Ask "What is the approval process?" → wait for SSE streaming to complete → verify ≥1 citation badge renders → click badge → verify navigation to Document Workspace → verify page N highlighted. Must complete in ≤2 clicks from citation to source page.

2. **Journey 5 — Research Workspace Sync:** Navigate to `/app/research` → ask question → wait for answer → click a citation badge → verify Evidence Panel shows source snippet WITHOUT navigating away (no URL change).

3. **Journey 4 — Compare and Diff:** Upload two documents (or use seeded) → navigate to Compare → select both versions → submit → wait for COMPLETED → verify ChangeItems render → click "View source" on a MAJOR change → verify navigation to correct Document Workspace page.

4. **Journey 1 — Upload to READY:** Upload a PDF → verify `ProcessingIndicator` badge appears in header → wait for status READY → navigate to document workspace → verify `PdfViewer` canvas element is present (not an `<iframe>`).

5. **Journey 6 — Conflict Resolution:** Navigate to `/app/conflicts` (seeded conflict present) → open detail → verify two evidence statements → resolve as REVIEWED → verify conflict tab moves to "Reviewed".

---

## 10. Security Review

Phase 15 does not own security hardening (Phase 16). The following items must be verified before Phase 15 is closed:

| Check | Status | Action |
|---|---|---|
| Tokens not in localStorage | `authStore.ts` stores access token in memory only; refresh is an httpOnly cookie | ✅ — verified; no change needed |
| Deep-link params treated as untrusted | `?page=N` parsed as integer with bounds check; `?q=text` used only as substring (no innerHTML/eval) | ✅ — safe; verify in PdfViewer implementation |
| No secrets in frontend bundle | `VITE_API_BASE_URL` is the only env var; no API keys | Verify in `vite.config.ts` |
| Permission checks from backend only | `authStore.currentUser.permissions` sourced from `GET /auth/me` — backend decision | ✅ — correct pattern |
| Settings endpoints org-isolated | `GET /settings/users` uses `current_user.organization_id` from JWT, never a client param | Verify in implementation |
| Audit log org-isolated | `GET /audit-logs` WHERE clause uses org from JWT | Verify in implementation |
| Command palette data scope | Uses same React Query cache as rest of app — already org-scoped at API level | ✅ — no additional surface |

---

## 11. Implementation Order

### Track A — Foundation (no dependencies; start immediately)

1. **A1** — Create `uiStore.ts` (sidebarCollapsed, commandPaletteOpen, documentViewMode, localStorage persistence)
2. **A2** — Create `panelStore.ts` (panel widths, activeCitation, persistence)
3. **A3** — Add `PermissionDenied.tsx` shared component
4. **A4** — Expand `AppShell.tsx`: sidebar collapse + UserMenu dropdown + Breadcrumbs + Suspense wrapper
5. **A5** — Convert `App.tsx` imports to `React.lazy()` + add NotFoundPage

### Track B — Backend Settings API (parallel with A; no frontend dependency)

1. **B1** — Create `backend/app/api/settings.py` with all 6 endpoints
2. **B2** — Add `UserRepository.list_by_org()` and `update_profile()` methods
3. **B3** — Add `AuditLogRepository.list_by_org()` method
4. **B4** — Register settings router in `main.py`
5. **B5** — Add `?days` param to `GET /analytics/summary`
6. **B6** — Add `document_a_id`/`document_b_id` to `ComparisonResponse`
7. **B7** — Create `frontend/src/lib/api/search.ts` (no backend change)
8. **B8** — Write `tests/api/test_settings.py` and `tests/api/test_audit_logs.py`

### Track C — Documents Feature (depends on A5 for route)

1. **C1** — Install `pdfjs-dist`; configure worker in `main.tsx`
2. **C2** — Create `PdfViewer.tsx` (canvas + text layer; one page at a time)
3. **C3** — Replace iframe in `DocumentWorkspace.tsx` with `PdfViewer`
4. **C4** — Add version selector to `DocumentWorkspace.tsx`; extract `useDocumentVersions`
5. **C5** — Add scrollspy to `TocPanel.tsx`
6. **C6** — Create `DocumentSearchBar.tsx`; intercept Ctrl+F in workspace
7. **C7** — Create `DocumentsPage.tsx` (library page replacing PlaceholderPage)
8. **C8** — Create `UploadDialog.tsx`; add `useUploadDocument()`, `useDocumentList()`, `useDeleteDocument()`
9. **C9** — Add `getDocumentContentApi()` to `documents.ts`

### Track D — Research Workspace (depends on A1/A2 for stores)

1. **D1** — Create `ResearchWorkspace.tsx` with three-panel CSS layout
2. **D2** — Create `research.css` with panel layout + resize handles + responsive rules
3. **D3** — Create `ResearchTranscript.tsx` (center panel — reuse AskPage chat logic)
4. **D4** — Create `ScopeChips.tsx` (left panel)
5. **D5** — Create `EvidencePanel.tsx` (right panel — reads panelStore.activeCitation)
6. **D6** — Wire citation click in transcript to `panelStore.setActiveCitation()` (instead of navigation)
7. **D7** — Add `/app/research` route in `App.tsx`; add viewport-width redirect guard

### Track E — Search + Settings (depends on B1/B7 for API clients)

1. **E1** — Create `useSearch.ts` hook
2. **E2** — Create `SearchPage.tsx` + `SearchResultCard.tsx` + `search.css`
3. **E3** — Create `frontend/src/lib/api/settings.ts` + `useSettings.ts`
4. **E4** — Create `SettingsLayout.tsx` + `settings.css`
5. **E5** — Create `ProfilePage.tsx`, `OrganizationPage.tsx`, `UsersPage.tsx`, `RolesPage.tsx`
6. **E6** — Create `AuditLogPage.tsx`
7. **E7** — Create placeholder Settings pages (AI, Document, Integrations, Security)
8. **E8** — Replace settings PlaceholderPage routes with full settings route tree in `App.tsx`

### Track F — Command Palette + Dashboard (depends on A1)

1. **F1** — Create `CommandPalette.tsx` + `CommandPalette.css`
2. **F2** — Mount `<CommandPalette>` in `AppShell.tsx`; add ⌘K/Ctrl+K global listener
3. **F3** — Extract `DashboardPage` to `features/dashboard/DashboardPage.tsx`
4. **F4** — Wire all 4 KPI cards live
5. **F5** — Add Recent Documents + Recent Questions + Processing Activity widgets

### Track G — Polish + Quality (after all tracks; confirm order)

1. **G1** — Compare: add "View source" buttons to `ChangeItem` using enriched ComparisonResponse
2. **G2** — Conflicts: verify/add "Compare Sources" button in `ConflictDetailPage`
3. **G3** — Summary: verify/add citation badges per bullet in `SummarySection`
4. **G4** — Analytics: range selector + quality bars + per-widget errors + "view as table"
5. **G5** — State-matrix audit: verify every screen × every state; file issues for blank/broken states
6. **G6** — Responsive CSS pass: add media queries for all new components
7. **G7** — Accessibility pass: ARIA, focus management, keyboard shortcuts
8. **G8** — Vitest setup + component tests
9. **G9** — Playwright E2E tests for Journey 2 and Journey 5

---

## 12. Files to Create / Modify

### Frontend — New Files (34 total)

| File | Requirement |
|---|---|
| `frontend/src/store/uiStore.ts` | §6.1.1, §6.10 |
| `frontend/src/store/panelStore.ts` | §6.5 |
| `frontend/src/components/layout/Breadcrumbs.tsx` | §6.1.3 |
| `frontend/src/components/CommandPalette.tsx` | §6.10 |
| `frontend/src/components/CommandPalette.css` | §6.10 |
| `frontend/src/components/PermissionDenied.tsx` | §6.11 |
| `frontend/src/features/dashboard/DashboardPage.tsx` | §6.2 |
| `frontend/src/features/documents/DocumentsPage.tsx` | §6.3 |
| `frontend/src/features/documents/UploadDialog.tsx` | §6.3 |
| `frontend/src/features/documents/PdfViewer.tsx` | §6.4.1 |
| `frontend/src/features/documents/DocumentSearchBar.tsx` | §6.4.4 |
| `frontend/src/features/research/ResearchWorkspace.tsx` | §6.5 |
| `frontend/src/features/research/EvidencePanel.tsx` | §6.5 |
| `frontend/src/features/research/ResearchTranscript.tsx` | §6.5 |
| `frontend/src/features/research/ScopeChips.tsx` | §6.5 |
| `frontend/src/features/research/research.css` | §6.5 |
| `frontend/src/features/search/SearchPage.tsx` | §6.6 |
| `frontend/src/features/search/SearchResultCard.tsx` | §6.6 |
| `frontend/src/features/search/search.css` | §6.6 |
| `frontend/src/features/settings/SettingsLayout.tsx` | §6.9 |
| `frontend/src/features/settings/ProfilePage.tsx` | §6.9 |
| `frontend/src/features/settings/OrganizationPage.tsx` | §6.9 |
| `frontend/src/features/settings/UsersPage.tsx` | §6.9 |
| `frontend/src/features/settings/RolesPage.tsx` | §6.9 |
| `frontend/src/features/settings/AISettingsPage.tsx` | §6.9 |
| `frontend/src/features/settings/DocumentSettingsPage.tsx` | §6.9 |
| `frontend/src/features/settings/IntegrationsPage.tsx` | §6.9 |
| `frontend/src/features/settings/SecurityPage.tsx` | §6.9 |
| `frontend/src/features/settings/AuditLogPage.tsx` | §6.9 |
| `frontend/src/features/settings/settings.css` | §6.9 |
| `frontend/src/lib/api/search.ts` | §6.6 |
| `frontend/src/lib/api/settings.ts` | §6.9 |
| `frontend/src/hooks/queries/useSearch.ts` | §6.6 |
| `frontend/src/hooks/queries/useSettings.ts` | §6.9 |

### Frontend — Modified Files (11 total)

| File | What Changes |
|---|---|
| `frontend/src/App.tsx` | React.lazy() for all routes; new routes (research, documents, search, settings tree); remove PlaceholderPages |
| `frontend/src/components/layout/AppShell.tsx` | Sidebar collapse; UserMenu; Breadcrumbs; CommandPalette trigger; Suspense wrapper |
| `frontend/src/features/documents/DocumentWorkspace.tsx` | Replace iframe with PdfViewer; version selector; DocumentSearchBar; 403 handler |
| `frontend/src/features/documents/TocPanel.tsx` | Scrollspy: accept currentPage, highlight active section |
| `frontend/src/features/analytics/AnalyticsPage.tsx` | Range selector; quality bars; per-widget errors; "view as table" toggle |
| `frontend/src/features/documents/ComparisonPage.tsx` | "View source" buttons in ChangeItem |
| `frontend/src/features/conflicts/ConflictDetailPage.tsx` | Verify/add "Compare Sources" button |
| `frontend/src/features/summary/SummarySection.tsx` | Verify/add citation badges per bullet |
| `frontend/src/hooks/queries/useDocuments.ts` | Add useDocumentList(), useUploadDocument(), useDeleteDocument(); extract useDocumentVersions |
| `frontend/src/lib/api/documents.ts` | Add getDocumentContentApi(); add filter params to listDocumentsApi |
| `frontend/src/main.tsx` | Configure PDF.js GlobalWorkerOptions.workerSrc |
| `frontend/package.json` | Add pdfjs-dist, vitest, testing libraries |

### Backend — New Files (1 total)

| File | Why |
|---|---|
| `backend/app/api/settings.py` | Settings + audit-log endpoints |

### Backend — Modified Files (5 total)

| File | What Changes |
|---|---|
| `backend/app/main.py` | Register settings router |
| `backend/app/api/analytics.py` | Add optional `?days` param |
| `backend/app/schemas/comparison.py` | Add `document_a_id`, `document_b_id` |
| `backend/app/api/compare.py` | Populate new doc ID fields |
| `backend/app/repositories/user_repository.py` | Add `list_by_org()`, `update_profile()` |
| `backend/app/repositories/refresh_token_repository.py` | Add `AuditLogRepository.list_by_org()` |

### Database Migrations

**None.** See §4 for full verification.

### Test Files (New)

| File | Coverage |
|---|---|
| `backend/tests/api/test_settings.py` | All settings endpoints; 403 matrix; org-isolation |
| `backend/tests/api/test_audit_logs.py` | Audit log read; filtering; org-isolation |
| `frontend/vitest.config.ts` | Vitest setup |
| `frontend/src/components/CommandPalette.test.tsx` | Keyboard nav; ARIA |
| `frontend/src/features/documents/DocumentsPage.test.tsx` | State matrix |
| `frontend/src/features/settings/AuditLogPage.test.tsx` | Filter; permissions |
| `frontend/tests/e2e/journey2.spec.ts` (Playwright) | Release gate: citation → source page ≤2 clicks |
| `frontend/tests/e2e/journey5.spec.ts` (Playwright) | Research Workspace citation sync |

---

## 13. Acceptance Criteria

### Application Shell

- [ ] Sidebar collapses/expands on button click; state persists across page refreshes
- [ ] `⌘K` / `Ctrl+K` opens command palette from any page; `Escape` closes it
- [ ] Breadcrumbs appear on routes deeper than level 1 (Document Workspace, Summary, Extraction Detail, etc.)
- [ ] User menu dropdown opens on click; "Settings" navigates; "Sign out" calls logout
- [ ] Processing indicator appears/disappears correctly per active job state
- [ ] No `PlaceholderPage` remains in the route tree

### Dashboard

- [ ] All 4 KPI cards show live data from real API calls
- [ ] Recent Documents widget shows up to 5 most recently updated documents as links
- [ ] Recent Questions widget shows up to 5 most recent conversations as links
- [ ] Processing Activity widget shows in-progress jobs; failed jobs show Retry
- [ ] New-organization empty state (zero docs, zero questions) shows upload CTA

### Documents Library

- [ ] `/app/documents` renders the full library (no PlaceholderPage)
- [ ] Upload dialog accepts PDF/DOCX; rejects other types with inline validation error
- [ ] Uploading triggers processing indicator; document appears in list
- [ ] Click a document navigates to its workspace

### Document Workspace

- [ ] PDF renders using PDF.js canvas (not `<iframe>`); text layer is selectable
- [ ] Zoom controls work (fit-width, fit-page, percentages)
- [ ] Version selector is present; switching versions reloads viewer and TOC
- [ ] TOC highlights active section as PDF page changes
- [ ] `Ctrl+F` / `⌘F` opens in-document search; F3/Shift+F3 navigate matches; match counter shows "N / M"
- [ ] `?page=N&q=text` deep link opens at page N with text highlighted
- [ ] 403 → `<PermissionDenied>` rendered (not a generic error card)

### Research Workspace

- [ ] `/app/research` renders three-panel layout at ≥1280px
- [ ] Below 1024px redirects to `/app/ask`
- [ ] Panel widths are resizable; widths persist across page refreshes
- [ ] Asking a question streams an answer with citation badges
- [ ] **Citation badge click updates Evidence Panel without navigating away from Research Workspace**

### Search

- [ ] `/app/search` renders the full search page (no PlaceholderPage)
- [ ] Mode toggle changes `mode` param in the API request
- [ ] Results update with 300ms debounce; no request for queries < 2 chars
- [ ] Clicking a result navigates to correct document page with highlight
- [ ] Empty/no-results/loading states render correctly

### Compare / Conflicts / Summary

- [ ] `ChangeItem` has "View source" buttons navigating to correct document page
- [ ] `ConflictDetailPage` has "Compare Sources" button that pre-fills the correct version pair
- [ ] Summary bullets render with citation badges that navigate to source page
- [ ] Comparison polling stops at COMPLETED/FAILED

### Analytics

- [ ] Range selector (7d/30d/90d/All) updates the displayed data
- [ ] Quality metrics display as CSS progress bars
- [ ] Each KPI card handles its own error state independently
- [ ] "View as table" toggle renders data as a `<table>` element

### Settings

- [ ] All 9 sections render without errors
- [ ] Profile page saves successfully; header name updates
- [ ] Organization page: admin can edit org name
- [ ] Users page: lists organization members; admin can assign roles
- [ ] Audit Log: shows events in descending order; action/date filters work
- [ ] Non-admin sees only Profile section; other sections show `<PermissionDenied>`

### Command Palette

- [ ] Opens with ⌘K/Ctrl+K; closes with Escape
- [ ] Documents, conversations, and quick actions appear in results
- [ ] Keyboard navigation works (`↑↓` + `Enter`)
- [ ] `aria-activedescendant` tracks focused item; result count announced by screen reader

### State Matrix

- [ ] Every screen in §6.11 table shows no blank white area or JavaScript error for any state

### Responsive

- [ ] Mobile (<768px): sidebar hidden, hamburger opens drawer
- [ ] Research Workspace: two-panel at 768–1279px; redirects at <1024px

### Accessibility

- [ ] All new modals are focus-trapped; focus returns to trigger on close
- [ ] Core journeys completable keyboard-only (no mouse)
- [ ] Lighthouse a11y score ≥90 on Dashboard, Document Workspace, Ask AI, Research Workspace

### Performance

- [ ] No page-specific code in the initial bundle (verify with `vite build --report`)
- [ ] Route chunks loaded lazily (verifiable in browser DevTools Network tab)

### E2E Journey Gates

- [ ] **Journey 2:** Question → streaming answer → citation badge click → source page with highlight in ≤2 clicks
- [ ] **Journey 5:** Research Workspace citation click → Evidence Panel shows source snippet without navigation
- [ ] **Journey 4:** Compare two versions → COMPLETED → "View source" navigates correctly
- [ ] **Journey 1:** Upload PDF → READY status → `PdfViewer` canvas renders
- [ ] **Journey 6:** Conflict → resolve as REVIEWED → moves to Reviewed tab

---

## 14. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **PDF.js integration complexity** — large API surface; text layer + in-doc search is non-trivial | High | High | Implement incrementally: canvas rendering first → text layer → in-doc search. Pin `pdfjs-dist` version. Use official ESM build. |
| **Research Workspace panel resize** — pointer event drag handling may have cross-browser issues | Medium | Medium | Simple `mousedown`/`mousemove`/`mouseup` on the resize handle div. Test on Chrome + Firefox + Edge. No third-party resize library for V1. |
| **Consolidation scope creep** — Phase 15 risks accumulating "nice to have" items from Phases 16–20 | High | High | The seven journey E2E tests and state-matrix audit are the scope boundary. Anything not required for them is explicitly out of scope. |
| **Summary citations missing from backend payload** — if `SummaryPayload` items don't carry citation data, citation badges can't render | Medium | High | Inspect `backend/app/schemas/summary.py` and `summary_service.py` early in implementation. If citation data is absent, this is a backend contract fix (not new AI work). |
| **API contract drift** — `lib/api/*.ts` types may not match actual backend response shapes | Medium | High | Run the frontend against the live backend early. Fix the smaller side (backend schema or frontend type). |
| **Large PDF performance** — 500-page PDF in PDF.js may be slow | Medium | Medium | Render one page at a time + 2 pages pre-load. Do not pre-render the full document. |
| **Accessibility retrofit pain** — existing components may have a11y issues | Medium | Medium | Start a11y audit early (Track G); fix incrementally; prioritize the seven journey paths. |

---

## 15. GLM Implementation Checklist

Execute in order within each track; tracks are parallel. Each item is independently testable.

### Track A — Foundation

- [ ] **A1** Create `uiStore.ts` with `sidebarCollapsed`, `commandPaletteOpen`, `documentViewMode`; persist to localStorage via Zustand persist
- [ ] **A2** Create `panelStore.ts` with `leftWidth`, `rightWidth`, `leftCollapsed`, `rightCollapsed`, `activeCitation`; persist panel dimensions
- [ ] **A3** Create `PermissionDenied.tsx` shared component
- [ ] **A4.1** Add sidebar collapse button to `AppShell.tsx` with `.app-sidebar--collapsed` CSS modifier
- [ ] **A4.2** Add `UserMenu` dropdown to `AppShell.tsx` header (Profile, Settings, Sign out links)
- [ ] **A4.3** Create `Breadcrumbs.tsx` using `useMatches()`; add `handle: { crumb }` to routes in `App.tsx`
- [ ] **A4.4** Add `CommandPaletteButton` to `AppShell.tsx` header
- [ ] **A4.5** Wrap `<Outlet>` in `AppShell.tsx` with `<React.Suspense fallback={<PageLoadingSpinner />}>`
- [ ] **A5.1** Convert all page imports in `App.tsx` to `React.lazy()`
- [ ] **A5.2** Replace `PlaceholderPage` in `path="*"` with `NotFoundPage`

### Track B — Backend

- [ ] **B1** Create `backend/app/api/settings.py` with all endpoint stubs
- [ ] **B2** Implement `PATCH /settings/profile` — update `users.full_name`
- [ ] **B3** Add `UserRepository.update_profile(user_id, full_name)` if missing
- [ ] **B4** Implement `PATCH /settings/organization` — update `organizations.name`; requires `org:manage`
- [ ] **B5** Implement `GET /settings/users` with pagination; requires `org:manage`; org-isolated
- [ ] **B6** Add `UserRepository.list_by_org(org_id, limit, offset)` if missing
- [ ] **B7** Implement `PATCH /settings/users/{user_id}/roles` with self-lockout guard; emit `PERMISSION_CHANGED` audit event
- [ ] **B8** Implement `GET /settings/roles`; requires `org:manage`
- [ ] **B9** Implement `GET /audit-logs` with all query params; requires `org:manage`; org-isolated WHERE clause
- [ ] **B10** Add `AuditLogRepository.list_by_org(org_id, filters, limit, offset)` with user email JOIN
- [ ] **B11** Register settings router in `backend/app/main.py`
- [ ] **B12** Add optional `?days: int = Query(None)` to `GET /analytics/summary`; backward-compatible
- [ ] **B13** Add `document_a_id: UUID`, `document_b_id: UUID` to `backend/app/schemas/comparison.py` `ComparisonResponse`
- [ ] **B14** Populate new doc ID fields in `backend/app/api/compare.py` via JOIN
- [ ] **B15** Update `frontend/src/lib/api/comparison.ts` `ComparisonResponse` with new fields
- [ ] **B16** Create `frontend/src/lib/api/search.ts` with all types and `searchDocuments()` function
- [ ] **B17** Write `backend/tests/api/test_settings.py` (all endpoints, 403 matrix, org-isolation)
- [ ] **B18** Write `backend/tests/api/test_audit_logs.py` (filtering, org-isolation, 403)

### Track C — Documents Feature

- [ ] **C1** Install `pdfjs-dist`; add to `package.json`
- [ ] **C2** Configure `GlobalWorkerOptions.workerSrc` in `frontend/src/main.tsx`
- [ ] **C3** Create `PdfViewer.tsx` (canvas + text layer; one page at a time; zoom controls)
- [ ] **C4** Replace `<iframe>` in `DocumentWorkspace.tsx` with `<PdfViewer>`
- [ ] **C5** Add version dropdown to `DocumentWorkspace.tsx` using `useDocumentVersions`
- [ ] **C6** Extract `useDocumentVersions` from `useComparisons.ts` to `useDocuments.ts`
- [ ] **C7** Implement scrollspy in `TocPanel.tsx` (accept `currentPage` prop; highlight active section; `onSectionClick` callback)
- [ ] **C8** Create `DocumentSearchBar.tsx`; intercept `Ctrl+F`/`⌘F` in workspace container
- [ ] **C9** Add `getDocumentContentApi(documentId, { chunk_id, page })` to `documents.ts`
- [ ] **C10** Add 403 handler to `DocumentWorkspace.tsx` → render `<PermissionDenied>`
- [ ] **C11** Create `DocumentsPage.tsx` replacing PlaceholderPage
- [ ] **C12** Add `useDocumentList(params)`, `useUploadDocument()`, `useDeleteDocument()` to `useDocuments.ts`
- [ ] **C13** Create `UploadDialog.tsx` (drag-drop, metadata form, multipart submit)
- [ ] **C14** Add route `/app/documents` → `<DocumentsPage>` in `App.tsx`

### Track D — Research Workspace

- [ ] **D1** Create `ResearchWorkspace.tsx` with three-panel CSS Grid layout
- [ ] **D2** Create `research.css` with panel styles, resize handles, responsive rules
- [ ] **D3** Add viewport-width redirect guard in `ResearchWorkspace.tsx` (redirect to `/app/ask` when `window.innerWidth < 1024`)
- [ ] **D4** Create `ScopeChips.tsx` for left panel scope selection
- [ ] **D5** Create `ResearchTranscript.tsx` (center panel — reuse AskPage streaming logic)
- [ ] **D6** Wire citation click in transcript to `panelStore.setActiveCitation(citation)` (not page navigation)
- [ ] **D7** Create `EvidencePanel.tsx` (right panel; reads `panelStore.activeCitation`; calls `getDocumentContentApi`)
- [ ] **D8** Add `/app/research` route in `App.tsx`

### Track E — Search + Settings

- [ ] **E1** Create `useSearch.ts` with debounced hook
- [ ] **E2** Create `SearchPage.tsx` (mode toggle, filter bar, debounced results, all states)
- [ ] **E3** Create `SearchResultCard.tsx`
- [ ] **E4** Create `search.css`
- [ ] **E5** Add route `/app/search` → `<SearchPage>` (remove PlaceholderPage)
- [ ] **E6** Create `frontend/src/lib/api/settings.ts`
- [ ] **E7** Create `frontend/src/hooks/queries/useSettings.ts`
- [ ] **E8** Create `SettingsLayout.tsx` with permission-gated left nav
- [ ] **E9** Create `ProfilePage.tsx` (form + save + authStore refresh)
- [ ] **E10** Create `OrganizationPage.tsx`
- [ ] **E11** Create `UsersPage.tsx` (table + role assignment modal)
- [ ] **E12** Create `RolesPage.tsx` (roles + permissions display)
- [ ] **E13** Create `AuditLogPage.tsx` (filter bar + paginated table + CSV export)
- [ ] **E14** Create `AISettingsPage.tsx`, `DocumentSettingsPage.tsx`, `IntegrationsPage.tsx`, `SecurityPage.tsx` as documented placeholders
- [ ] **E15** Create `settings.css`
- [ ] **E16** Replace settings PlaceholderPage routes in `App.tsx` with full nested route tree

### Track F — Command Palette + Dashboard

- [ ] **F1** Create `CommandPalette.tsx` (focus-trap, keyboard nav, ARIA, sections)
- [ ] **F2** Create `CommandPalette.css`
- [ ] **F3** Mount `<CommandPalette>` in `AppShell.tsx`; add ⌘K/Ctrl+K global keydown listener
- [ ] **F4** Extract `DashboardPage` to `features/dashboard/DashboardPage.tsx`
- [ ] **F5** Wire all 4 KPI cards with live API data
- [ ] **F6** Add Recent Documents widget
- [ ] **F7** Add Recent Questions widget
- [ ] **F8** Add Processing Activity widget with failed-jobs Retry
- [ ] **F9** Add new-organization empty state

### Track G — Polish + Quality

- [ ] **G1** Comparison: add "View source" buttons to `ChangeItem` using `document_a_id`/`document_b_id`
- [ ] **G2** Conflicts: verify/add "Compare Sources" button in `ConflictDetailPage`
- [ ] **G3** Summary: verify/add citation badges per bullet in `SummarySection`
- [ ] **G4** Analytics: range selector + quality CSS bars + per-widget errors + "view as table" toggle
- [ ] **G5** State-matrix audit: complete the §6.11 table; file issues for any broken state
- [ ] **G6** Responsive CSS: add media queries for sidebar, Research Workspace, Settings, Dashboard, Command Palette
- [ ] **G7** Accessibility: ARIA attributes, focus management, keyboard shortcuts on all new components
- [ ] **G8** Install Vitest + testing-library; create `vitest.config.ts`
- [ ] **G9** Write component tests: `CommandPalette`, `DocumentsPage`, `AuditLogPage`, `UploadDialog`, `ProcessingStatusTracker`
- [ ] **G10** Install Playwright; write `journey2.spec.ts` (citation → source ≤2 clicks)
- [ ] **G11** Write `journey5.spec.ts` (Research Workspace citation sync)
- [ ] **G12** Run all Journey 2–6 E2E tests against seeded environment; fix failures

---

*End of Plan*
*New frontend files: 34 | Modified frontend files: 12 | New backend files: 1 | Modified backend files: 6 | New migrations: 0*
