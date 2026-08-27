# AI Document Intelligence Platform — Frontend Design Documentation

**Document type:** Implementation-ready frontend design specification
**Audience:** Frontend engineers, product designers, QA, backend integrators
**Stack:** React + TypeScript + React Query + Tailwind CSS + PDF.js + FastAPI (REST) + WebSocket/SSE
**Status:** v1.0 — Draft for engineering handoff
**Owner:** Product Design / Frontend Architecture

> This document is a specification, not an implementation. It defines the structure, states, interactions, data contracts, and visual system needed to build the product without the engineering team having to make major UX decisions on their own.

---

## Table of Contents

1. [Product Overview](#1-product-overview)
2. [UX Goals](#2-ux-goals)
3. [Design Principles](#3-design-principles)
4. [Information Architecture](#4-information-architecture)
5. [Application Navigation & Layout](#5-application-navigation--layout)
6. [Screen-by-Screen Design](#6-screen-by-screen-design)
   - [6.1 Authentication](#61-authentication)
   - [6.2 Dashboard](#62-dashboard)
   - [6.3 Documents Page](#63-documents-page)
   - [6.4 Document Upload Experience](#64-document-upload-experience)
   - [6.5 Document Details / Document Workspace](#65-document-details--document-workspace)
   - [6.6 AI Chat / Ask AI](#66-ai-chat--ask-ai)
   - [6.7 AI Answer + Citation Experience](#67-ai-answer--citation-experience)
   - [6.8 Three-Panel Research Workspace](#68-three-panel-research-workspace)
   - [6.9 Search](#69-search)
   - [6.10 Document Comparison](#610-document-comparison)
   - [6.11 Change Detection](#611-change-detection)
   - [6.12 Document Summary](#612-document-summary)
   - [6.13 Conflict Detection](#613-conflict-detection)
   - [6.14 Analytics](#614-analytics)
   - [6.15 Settings](#615-settings)
7. [User Journeys](#7-user-journeys)
8. [Component Architecture](#8-component-architecture)
9. [State Management](#9-state-management)
10. [API Integration](#10-api-integration)
11. [Real-Time Processing UX](#11-real-time-processing-ux)
12. [Citation UX (Deep Dive)](#12-citation-ux-deep-dive)
13. [Document Viewer UX (Deep Dive)](#13-document-viewer-ux-deep-dive)
14. [Comparison UX (Deep Dive)](#14-comparison-ux-deep-dive)
15. [Responsive Design](#15-responsive-design)
16. [Accessibility](#16-accessibility)
17. [Design System](#17-design-system)
18. [Error / Loading / Empty States](#18-error--loading--empty-states)
19. [Frontend Architecture](#19-frontend-architecture)
20. [Future UX Enhancements](#20-future-ux-enhancements)

---

## 1. Product Overview

The **AI Document Intelligence Platform** is an enterprise workspace for organizations to ingest, understand, and reason over large bodies of business documentation — policies, SOPs, contracts, regulatory filings, technical specs, and scanned records.

Unlike a "chat with your PDF" tool, this product treats **documents as first-class, persistent objects** with identity, structure, versions, ownership, and lifecycle state. AI capabilities (chat, search, summarization, comparison, conflict detection) are **lenses applied on top of the document corpus**, not the product itself.

**Primary users:**

| Role | Goal |
|---|---|
| Compliance / Legal Analyst | Find the exact clause, cite it precisely, detect version drift |
| Operations / SOP Owner | Keep procedures current, understand what changed between revisions |
| Knowledge Worker | Ask natural-language questions and trust the answer because it's sourced |
| Org Admin | Manage users, permissions, collections, and ingestion pipeline health |

**What makes this different from a chatbot:**

- Every AI answer is grounded in retrievable, citable source text.
- Documents have structure (sections, pages, versions) that the UI exposes directly.
- Users can work *with* a specific document, a *set* of documents, or the *whole* knowledge base — and always know which scope is active.
- Change and conflict across versions/documents are modeled as data, not just prose the AI happens to mention.

---

## 2. UX Goals

1. **Explainability first.** Every AI-surfaced claim must be one click away from its exact source text, page, and section.
2. **Documents as anchors.** Users should always know *which document(s)* they are working with — scope is never implicit.
3. **Trustworthy under uncertainty.** When the system isn't sure (low relevance, no citation, conflicting sources), the UI says so — it never hides ambiguity behind a confident-sounding sentence.
4. **Information density without clutter.** This is a professional tool used for hours at a time; favor scanability (tables, badges, structure) over decorative UI.
5. **Predictable states.** Loading, empty, error, and partial states are designed with the same care as the success state — never an afterthought.
6. **Fast orientation.** A new user should understand "where am I, what's selected, what can I do" within seconds of landing on any screen.
7. **Progressive disclosure.** Summary first, evidence on demand — never dump raw chunks or vector scores in the primary view.

---

## 3. Design Principles

### 3.1 The Explainability Principle (governing principle of the whole product)

> The application must always let the user answer: **what** did the AI say, **which documents** were used, **which pages/sections**, **what exact text** supports it, **when was it effective**, and **is there conflicting information**.

Concretely, this means:

- No AI-generated sentence should be exempt from a citation affordance if it makes a factual claim.
- Citations are never a footnote-only pattern — they are interactive, hoverable, and clickable, landing the user in the source document at the exact passage.
- Every comparison and conflict view shows source snippets, not just a described difference.
- "No citation available" is a visible, explicit state — never silently omitted.

### 3.2 Documents Are Primary Objects

- The IA is rooted in **Documents**, not conversations. Chat, search, and comparison are entry points *into* documents, and every result routes back to a document.
- A conversation always displays its **document scope** persistently — never buried in a settings menu.

### 3.3 Enterprise Visual Restraint

- No gradient/glow "AI magic" styling, no animated sparkles, no chat-bubble-only interfaces.
- Structured layouts: tables, panels, badges, and tabs — the visual language of enterprise SaaS (e.g., Linear, Notion for Teams, Retool), not consumer chat apps.

### 3.4 Deterministic Before Generative

- Wherever a deterministic UI can answer a question (filters, table columns, sort, version diff), prefer it over a chat answer. Chat is for synthesis across documents, not a replacement for browsing.

### 3.5 Fail Loud, Fail Specific

- Processing failures, low-confidence answers, and permission issues are surfaced with specific, actionable messaging tied to the object involved — never a generic toast.

---

## 4. Information Architecture

### 4.1 Core Entity Model (as surfaced in the UI)

```text
Organization (tenant)
 └── Collections (folders/knowledge bases, e.g. "HR Policies", "Vendor Contracts")
      └── Documents
           ├── Versions (v2025, v2026 …)
           │     ├── Sections / Subsections (table of contents nodes)
           │     ├── Pages
           │     └── Chunks (retrieval units — not shown directly to users)
           └── Metadata (type, department, owner, status, tags, effective date)

Conversations (Ask AI sessions)
 └── Messages
      └── Citations → Document + Version + Page + Section + Source text span

Comparisons
 └── Document A version ↔ Document B version
      └── Changes (added / removed / modified) → Citations

Conflicts
 └── Statement A (doc/version/citation) ↔ Statement B (doc/version/citation)
```

### 4.2 Site Map

```text
/login
/register
/forgot-password

/app
 ├── /dashboard
 ├── /documents
 │    ├── /documents/:id                    (Document Workspace)
 │    └── /documents/upload
 ├── /ask                                    (Ask AI — default: knowledge base scope)
 │    └── /ask/:conversationId
 ├── /search
 ├── /compare
 │    └── /compare/:comparisonId
 ├── /research/:sessionId                    (Three-Panel Research Workspace, deep-link target)
 ├── /analytics
 └── /settings
      ├── /settings/profile
      ├── /settings/organization
      ├── /settings/users
      ├── /settings/roles
      ├── /settings/ai
      ├── /settings/documents
      ├── /settings/integrations
      ├── /settings/security
      └── /settings/audit-log
```

### 4.3 Navigation Hierarchy

- **Level 1 — Sidebar (global sections):** Dashboard, Documents, Ask AI, Search, Compare, Analytics, Settings.
- **Level 2 — Contextual tabs/sub-nav within a screen** (e.g., inside a Document Workspace: Overview / Table of Contents / Summary / Versions).
- **Level 3 — In-page anchors** (e.g., section jump-to within the TOC panel, citation jump-to within the PDF viewer).

Breadcrumbs are used only where depth > 2 (e.g., `Documents / Marketing Policy 2026 / v2.1`).

---

## 5. Application Navigation & Layout

### 5.1 Persistent App Shell

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Header: Logo | Global Search | Org Switcher | Notifications | Avatar │
├───────────────┬────────────────────────────────────────────────────┤
│ Sidebar       │                Main Workspace                       │
│ (collapsible) │        (routed page content, scrollable)            │
│               │                                                     │
│ ▸ Dashboard   │                                                     │
│ ▸ Documents   │                                                     │
│ ▸ Ask AI      │                                                     │
│ ▸ Search      │                                                     │
│ ▸ Compare     │                                                     │
│ ▸ Analytics   │                                                     │
│ ▸ Settings    │                                                     │
│               │                                                     │
│ [Collapse ⇤]  │                                                     │
└───────────────┴────────────────────────────────────────────────────┘
```

**Header (height 56px, fixed):**
- Left: product logo/wordmark → links to Dashboard.
- Center-left: global command/search bar (`⌘K`) — quick jump to documents, recent conversations, and pages.
- Right: processing-activity indicator (a subtle badge showing "3 processing" with a dropdown of live jobs), notifications bell, organization switcher (multi-tenant users), user avatar menu (Profile, Settings, Sign out).

**Sidebar (width 240px expanded / 64px collapsed, icon-only when collapsed):**
- Persistent across all `/app/*` routes.
- Active route highlighted with left accent bar + filled icon.
- Bottom-anchored: collapse toggle, help link, product version.
- Collapses automatically at the `lg` breakpoint (see [§15](#15-responsive-design)); user preference persisted in `localStorage`.

**Main Workspace:**
- Owns its own scroll container (header/sidebar stay fixed).
- Screens that need full-bleed space (Document Workspace, Research Workspace) suppress the default page padding and manage their own internal layout/scroll regions.

### 5.2 Global Processing Indicator

A small pill in the header (`● 2 processing`) is present whenever any document in the org is mid-pipeline. Clicking it opens a dropdown listing active jobs with live progress (reusing the `ProcessingStatus` component, [§8](#8-component-architecture)), each row linking to its document. This ensures processing status is never something the user has to go hunting for outside the Documents page.

### 5.3 Command Palette (`⌘K` / `Ctrl+K`)

- Fuzzy search across documents, saved searches, and recent conversations.
- Quick actions: "Upload document", "New comparison", "Ask AI".
- Fully keyboard-operable (see [§16](#16-accessibility)).

---

## 6. Screen-by-Screen Design

Each screen below is documented with: **Purpose, Layout, Components, Interactions, Data Required, API Dependencies, UX States, Responsive Behavior, Accessibility.**

---

### 6.1 Authentication

**Purpose:** Authenticate users and route them into the correct tenant workspace; recover access when credentials are lost.

**Screens:** Login, Registration, Forgot Password, Reset Password.

**Layout (all auth screens share a centered single-column card, max-width 420px, on a neutral background):**

```text
┌──────────────────────────────┐
│           Logo               │
│                               │
│  Email                       │
│  [___________________]      │
│                               │
│  Password                    │
│  [___________________]      │
│                               │
│  [ ] Remember me   Forgot?   │
│                               │
│  [        Sign in        ]  │
│                               │
│  ──────── or ────────        │
│  [  Continue with SSO   ]   │
│                               │
│  Don't have an account?      │
│  Create one                  │
└──────────────────────────────┘
```

**Components:** `AuthCard`, `TextInput`, `PasswordInput` (show/hide toggle), `Button`, `Checkbox`, `InlineAlert`, `SSOButton`.

**Interactions:**
- Inline validation on blur (email format, required fields); submit disabled until valid.
- Password field: visibility toggle, caps-lock warning.
- "Forgot password" → email-entry screen → confirmation screen ("check your email") → reset-password screen (token from URL).
- Registration collects: name, work email, organization name (or invite-token join), password (with strength meter), and requires ToS acceptance.
- SSO (SAML/OIDC) button when the org has it configured — routes to IdP redirect flow.

**Data required:** none pre-load; on submit, credentials POSTed to `/auth/login`.

**API dependencies:** `POST /auth/login`, `POST /auth/register`, `POST /auth/forgot-password`, `POST /auth/reset-password`, `GET /auth/sso/:orgSlug`.

**UX States:**
- **Loading:** submit button shows spinner + disabled state; form fields disabled.
- **Error:** inline banner above the form for auth failures ("Incorrect email or password") — never field-specific for login (avoid user enumeration); field-specific for registration (e.g., "Email already in use").
- **Success:** redirect to `/app/dashboard` (or `/app` last-visited route via post-login redirect param).
- **Rate-limited:** explicit message with retry countdown after N failed attempts.
- **Session expired (app-wide):** any authenticated request returning 401 redirects to `/login?reason=expired` with a banner: "Your session expired. Please sign in again."

**Responsive:** Fully responsive single-column; card becomes full-width with padding on mobile. Fully usable on mobile since users may need to approve/check status on the go.

**Accessibility:** Form labels always visible (not placeholder-only), `aria-invalid` + `aria-describedby` on error fields, error summary announced via `aria-live="assertive"` on submit failure, logical tab order, submit-on-Enter.

---

### 6.2 Dashboard

**Purpose:** Give an at-a-glance operational overview of the knowledge base — what exists, what's active, what needs attention — and route users to their next action fast.

**Layout:**

```text
┌────────────────────────────────────────────────────────────────┐
│ Welcome back, {name}                     [Upload] [Ask AI]     │
├───────────┬───────────┬───────────┬────────────────────────────┤
│ Documents │ Questions │Collections│ Storage                    │
│   128     │    842    │    12     │  4.2 GB                    │
├───────────┴───────────┴───────────┴────────────────────────────┤
│ Processing Activity (live)         │  Failed Jobs (2)          │
│ ● Vendor Contract.pdf  Embedding…  │  ✕ Scan_003.pdf  OCR fail │
│ ● SOP-114.docx        OCR…         │  ✕ Old_Policy   Extract   │
├─────────────────────────────────────────────────────────────────┤
│ Recently Uploaded                  │  Recent Questions          │
│ • Marketing Policy 2026   READY    │  "What is the approval…"  │
│ • HR Handbook v3          READY    │  "Summarize vendor terms" │
│ • Contract_Acme.pdf   PROCESSING   │  "Compare 2025 vs 2026"   │
└─────────────────────────────────────────────────────────────────┘
```

**Hierarchy:** KPI strip (top, glanceable) → operational attention area (processing + failures, needs-action) → recent activity (historical, browsable). This ordering surfaces *action-needed* content above *reference* content.

**Components:** `KpiCard` ×4, `QuickActionBar`, `ProcessingActivityList`, `FailedJobsList`, `RecentDocumentsList`, `RecentQuestionsList`, `EmptyState`.

**Interactions:**
- KPI cards are clickable → route to filtered Documents / Analytics views.
- Failed job rows → click opens the document's processing detail (with retry action).
- Recent question rows → click resumes that conversation in Ask AI.
- Quick actions: "Upload Document", "Ask AI", "New Comparison" pinned top-right.

**Data required:** org-level counts, last 5 processing jobs, last 5 failed jobs, last 5 uploaded documents, last 5 questions.

**API dependencies:** `GET /analytics/summary`, `GET /documents?sort=recent&limit=5`, `GET /documents/processing`, `GET /chat/recent?limit=5`.

**UX States:**
- **Loading:** skeleton KPI cards + skeleton list rows (shimmer), not a full-page spinner — dashboard loads progressively per widget.
- **Empty (new org, zero documents):** KPI strip shows zeros; main area replaced with a single centered `EmptyState`: "Your knowledge base is empty — upload your first document" + primary CTA.
- **Error:** per-widget error state ("Couldn't load recent activity — Retry") so one failed widget doesn't blank the whole page.
- **Partial:** if failed-jobs count is 0, that card collapses/hides rather than showing an empty box.

**Responsive:** KPI strip wraps to 2×2 grid on tablet, 1-column stack on mobile. Processing/Failed and Recent sections stack vertically below `md`.

**Accessibility:** KPI cards are `<button>`/link semantics with accessible names ("128 documents, view all"), live region for processing counts that update in place (polite, not assertive, to avoid interrupting screen reader users).

---

### 6.3 Documents Page

**Purpose:** The system of record for every document in the org — browse, filter, manage lifecycle, and act in bulk.

**Layout:**

```text
┌────────────────────────────────────────────────────────────────┐
│ Documents                                    [+ Upload]        │
│ [Search…] [Type ▾][Dept ▾][Status ▾][Collection ▾]  [Filters] │
├────────────────────────────────────────────────────────────────┤
│ [ ] Name          Type    Dept   Status    Ver  Owner   Updated│
│ [ ] Marketing…    Policy  Mktg   ● READY   2.1  J.Doe   2d ago │
│ [ ] SOP-114.docx  SOP     Ops    ◐ EMBED…  1.0  A.Lee   5m ago │
│ [ ] Scan_003.pdf  Scan    Legal  ✕ FAILED  1.0  R.Chen  1h ago │
├────────────────────────────────────────────────────────────────┤
│ 3 selected  [Move ▾] [Tag ▾] [Change access] [Delete]          │
│                                       ‹ 1 2 3 … 9 ›  20/page ▾ │
└────────────────────────────────────────────────────────────────┘
```

Grid view (toggle) shows the same data as `DocumentCard` tiles for visual browsing.

**Components:** `DocumentTable` / `DocumentCard`, `FilterBar`, `StatusBadge`, `BulkActionBar`, `Pagination`, `ColumnSortHeader`, `ViewToggle` (table/grid), `SearchInput`.

**Table columns:** checkbox, Name (+ file-type icon), Type, Department, Status, Version, Owner, Updated, row menu (`⋯`: Open, Ask AI, Download, Move, Archive, Delete).

**Document status values and visual treatment:**

| Status | Badge color | Icon | Meaning |
|---|---|---|---|
| `UPLOADED` | Gray | ○ | File received, queued |
| `PROCESSING` | Blue (pulsing dot) | ◐ | Pipeline started |
| `EXTRACTING` | Blue | ◐ | Text extraction in progress |
| `OCR` | Blue | ◐ | OCR running (scanned doc) |
| `EMBEDDING` | Blue | ◐ | Generating vector embeddings |
| `INDEXING` | Blue | ◐ | Writing to search index |
| `READY` | Green | ● | Fully processed, queryable |
| `FAILED` | Red | ✕ | Pipeline error — actionable |

All non-terminal states (`PROCESSING`→`INDEXING`) render as a single **"Processing"** badge family with a sub-label (e.g., `Processing · Embedding`) plus an inline mini progress bar, rather than 5 visually distinct badges — this keeps the table scannable while still being precise on hover/expand.

**Interactions:**
- Row click → Document Workspace (`/documents/:id`).
- Column headers sortable (Name, Updated, Status, Type); multi-column not required for v1.
- Filters combine with AND logic; active filters shown as removable chips below the filter bar.
- Search matches document name, extracted content (server-side), and metadata.
- Bulk selection → contextual action bar appears (replacing pagination row); actions: Move to Collection, Add Tags, Change Access, Archive, Delete (with confirmation modal listing affected doc count).
- `FAILED` rows expose a "Retry processing" action directly in the row menu.

**Data required:** paginated document list with metadata, applied filters/sort/search query, total count.

**API dependencies:** `GET /documents?query=&type=&department=&status=&collection=&sort=&page=&pageSize=`, `POST /documents/bulk` (move/tag/access/delete), `POST /documents/:id/retry`.

**UX States:**
- **Loading:** skeleton table rows (8) on first load; subtle top progress bar on subsequent filter/sort changes (rows stay visible, dimmed).
- **Empty (no documents at all):** centered `EmptyState` — "No documents yet" + Upload CTA.
- **No results (filters/search yield nothing):** distinct from true-empty — "No documents match your filters" + "Clear filters" action.
- **Error:** inline banner above table, table area shows retry state.
- **Partial (bulk action partially fails):** result toast: "8 of 10 moved — 2 failed (permission denied)" with a "View details" link.
- **Permission denied:** rows/actions the user's role can't perform are hidden or disabled with a tooltip ("Requires Editor role"), not silently absent — bulk bar only shows actions the user is authorized for.

**Responsive:** Below `md`, table collapses to a card list (one `DocumentCard` per row: name, status badge, updated date, and a `⋯` menu; secondary metadata tucked behind an expand). Filter bar collapses into a single "Filters" button opening a sheet.

**Accessibility:** table uses proper `<table>` semantics with `scope="col"`, sortable headers are buttons with `aria-sort`, checkboxes have accessible labels ("Select Marketing Policy 2026"), bulk action bar is announced via live region when selection count changes, status badges include text (not color-only).

---

### 6.4 Document Upload Experience

**Purpose:** Get one or many files into the pipeline with correct metadata and full visibility into ingestion progress.

**Layout (modal or dedicated `/documents/upload` route — modal for 1–2 files from Dashboard/Documents page, full route when arriving via deep link or for large batch uploads):**

```text
┌────────────────────────────────────────────────────────────┐
│ Upload Documents                                    [✕]    │
├──────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────┐  │
│  │        Drag & drop files here, or  [Browse]           │  │
│  │        PDF, DOCX, scanned images · up to 100MB each    │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                                │
│  Collection:  [ HR Policies          ▾ ]                    │
│  Access:      ( ) Organization  (•) Specific roles  ( ) Private │
│  Tags:        [ policy ] [ 2026 ] [+]                        │
│                                                                │
│  Files (3)                                                    │
│  📄 Marketing_Policy_2026.pdf   2.1 MB   [x]                 │
│     ✓ Uploaded  ✓ Extracted  ✓ Structure  ● Embeddings  ○ Index │
│  📄 SOP-114.docx                0.8 MB   [x]                 │
│     ✓ Uploaded  ● OCR n/a — extracting text…                 │
│  📄 Scan_003.pdf                4.4 MB   [x]                 │
│     ✕ Failed — unsupported encoding                          │
│                                                                │
│                              [Cancel]     [Close & continue] │
└──────────────────────────────────────────────────────────────┘
```

**Components:** `DocumentUpload` (dropzone), `FilePickerButton`, `UploadFileRow`, `ProcessingStatus` (per-file step tracker), `CollectionSelect`, `AccessLevelRadioGroup`, `TagInput`, `Button`.

**Workflow:**
1. **File intake** — drag/drop or file picker, multi-select supported. Client-side validation runs immediately: file type (PDF/DOCX/DOC/TXT/common image formats), size limit, duplicate-name detection against the target collection.
2. **Metadata** (applies to the batch, overridable per-file via an "Advanced" expand): Collection, Access level, Tags, Department, Owner (defaults to uploader).
3. **Upload** — files upload in parallel (max 3 concurrent) with per-file byte-progress bars; user can remove a file before it starts or cancel an in-flight upload.
4. **Processing** — once a file finishes uploading, its row switches from a progress bar to a **step tracker**:

```text
✓ File uploaded
✓ Text extracted
✓ Structure detected
✓ OCR completed
● Generating embeddings
○ Indexing
```

   Completed steps show `✓` (green), the active step shows `●` (blue, animated pulse) with an inline sub-status when available (e.g., "Generating embeddings · 340/512 chunks"), and pending steps show `○` (gray). A failed step shows `✕` (red) and halts subsequent steps, with an inline error reason and a "Retry" link.

5. **Completion** — the modal doesn't block the user from continuing to work; "Close & continue" dismisses it and the global header processing indicator ([§5.2](#52-global-processing-indicator)) picks up tracking. A toast confirms: "3 documents uploaded — 1 failed."

**Interactions:**
- Files can be added mid-session (drag more files onto an already-open modal).
- Reordering not needed; per-file remove (`x`) allowed until that file reaches `READY`.
- OCR step is conditionally shown only for image-based/scanned PDFs; text-native PDFs skip it (shown as N/A, not as a fake pending step).

**Data required:** target org's collections list, user's role (to constrain access-level options), max upload size/allowed types (from org settings).

**API dependencies:** `POST /documents` (multipart, one call per file, returns `documentId` immediately), `GET /collections`, then per-document progress via WebSocket/SSE (see [§11](#11-real-time-processing-ux)).

**UX States:**
- **Loading (uploading):** determinate progress bar per file (byte-based).
- **Processing:** step tracker as above, driven by streamed events.
- **Error (validation):** inline under the dropzone before upload starts (e.g., "Scan_003.pdf exceeds 100MB limit").
- **Error (processing failure):** step tracker halts at failed step with reason + Retry; document status becomes `FAILED` and appears as such on the Documents page.
- **Success:** step tracker fully checked, row collapses to a compact "Ready — View document" link.
- **Empty:** default dropzone prompt state before any file is added.
- **Network interruption mid-upload:** row shows "Upload interrupted — Retry" (auto-retry with backoff attempted twice before surfacing this).

**Responsive:** Modal becomes a full-screen sheet on mobile/tablet; drag-and-drop degrades gracefully to file-picker-only affordance (drag target still present but de-emphasized) since drag-and-drop is impractical on touch. Upload is a supported but not primary mobile use case.

**Accessibility:** dropzone is keyboard-reachable and operable via `Enter`/`Space` to open the file picker; each file row's step tracker is announced via `aria-live="polite"` on step change (throttled, not per-token); failed steps get `role="alert"`; file removal buttons have accessible names including the filename.

---

### 6.5 Document Details / Document Workspace

**Purpose:** The canonical "home" of a single document — read it, understand its structure, and launch every document-scoped AI capability from one place.

**Layout:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Marketing Policy 2026   v2.1 · ● READY · Policy · Marketing   │
│ Owner: J. Doe · Updated 2d ago     [Ask AI][Compare][Summary] │
│                                     [Download][⋯]              │
├───────────────────┬───────────────────────────────────────────┤
│ Overview           │  ┌─────────────────────────────────────┐ │
│ Table of Contents  │  │  Toolbar: [Search in doc] [⤢][Page 12/40]│
│ ▸ 1. Purpose       │  ├─────────────────────────────────────┤ │
│ ▸ 2. Scope         │  │                                     │ │
│ ▾ 3. Roles         │  │         PDF page render             │ │
│    3.1 Approvers   │  │      (with highlight overlay)       │ │
│    3.2 Reviewers   │  │                                     │ │
│ ▾ 4. Process        │  │                                     │ │
│    4.1 Submission   │  └─────────────────────────────────────┘ │
│  ► 4.2 Review [•]   │                                          │
│ Versions (3)        │                                          │
└───────────────────┴───────────────────────────────────────────┘
```

**Left panel tabs:** Overview (metadata: type, department, owner, effective date, tags, description, file info), Table of Contents (hierarchical section tree), Versions (version history list with diff/compare shortcuts).

**Components:** `DocumentHeader`, `DocumentMetadataPanel`, `TableOfContents`, `DocumentViewer` (PDF.js-based), `VersionHistoryList`, `DocumentActionBar`, `InDocumentSearch`.

**Document actions (header bar):**
- **Ask AI** → opens Ask AI pre-scoped to "Current document."
- **Compare** → opens Comparison screen with Document A pre-filled as this document, prompting for Document B.
- **Summary** → opens the Document Summary screen for this document/version.
- **Download** → original file download (respecting access permissions).
- **⋯ menu** → Rename, Move to collection, Change access, Archive, Delete, View audit history.

**Interactions:**
- TOC nodes are clickable and scroll/jump the viewer to that section's starting page; the current visible section auto-highlights in the TOC as the user scrolls (scrollspy behavior), and a `[•]` marker denotes a section with unresolved conflicts.
- In-document search highlights all matches with a match counter and next/prev navigation, distinct from knowledge-base Search ([§6.9](#69-search)).
- Version selector in the header lets the user switch which version's content is rendered; switching versions updates the TOC and viewer together, and shows a subtle banner if viewing a non-current version ("You are viewing v2.0 — Marketing Policy 2026 · [View latest]").

**Data required:** document metadata, version list, TOC tree, rendered pages/PDF asset URL, page-level text index for in-doc search.

**API dependencies:** `GET /documents/:id`, `GET /documents/:id/versions`, `GET /documents/:id/toc?version=`, `GET /documents/:id/content?version=` (signed asset URL), `GET /documents/:id/search?query=`.

**UX States:**
- **Loading:** header skeleton + TOC skeleton (5 lines) + viewer skeleton (page placeholder with spinner).
- **Processing (document not yet READY):** viewer area replaced with the same step-tracker component as upload ("This document is still processing — Ask AI and Search will be available once indexing completes"), read-only preview of the raw file still offered if extraction has completed enough to render pages.
- **Error (load failure):** full-panel error with Retry.
- **Error (FAILED document):** clear failure reason + "Retry processing" primary action; TOC/AI actions disabled with explanatory tooltips.
- **Empty (no TOC detected):** TOC panel shows "No structure detected for this document" and falls back to page-based navigation only.
- **Permission denied:** if the user lacks access, show a dedicated access-denied state (not a 404) with a "Request access" action where applicable.

**Responsive:** Desktop-first ([§15](#15-responsive-design)). Below `lg`, the left panel collapses into a slide-over drawer triggered by a "Contents" button in the toolbar; the viewer becomes the primary full-width surface. Below `md` (mobile), the PDF viewer supports pinch-zoom/pan and page-swipe navigation; heavy actions (Compare) are still available but route to a desktop-optimized flow with a "best viewed on larger screens" notice for the comparison view specifically.

**Accessibility:** TOC is a `<nav>` with a proper tree/list structure and `aria-current` on the active section; viewer toolbar controls are all keyboard operable; page number input allows direct keyboard entry; text layer from PDF.js preserves selectable/screen-reader-readable text (not just a rasterized image) whenever the source allows it.

---

### 6.6 AI Chat / Ask AI

**Purpose:** Let users ask natural-language questions with an explicit, adjustable document scope — not an open-ended chatbot.

**Layout:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Ask AI                                          [+ New chat]  │
├───────────────────┬───────────────────────────────────────────┤
│ Conversations      │ Scope: ● Selected documents (2)  [Edit]  │
│ • Approval process │───────────────────────────────────────────│
│ • Vendor terms Q&A │  You: What is the approval process?      │
│ • 2025 vs 2026 …   │                                            │
│                     │  AI: The approval process contains four  │
│                     │  stages. [1][2]                           │
│                     │  Sources: [1] Marketing Policy — p.12     │
│                     │           [2] Approval SOP — p.8          │
│                     │  [Regenerate] [Copy] [👍][👎]             │
│                     │                                            │
│                     │  Suggested: "Who approves stage 2?"       │
│                     │───────────────────────────────────────────│
│                     │ [Ask a question…]                [Send] │
└───────────────────┴───────────────────────────────────────────┘
```

**Scope selector (critical UX element, always visible above the message list):**

```text
Scope
( ) Current document        — only when opened from a Document Workspace
(•) Selected documents (2)
( ) Entire knowledge base

✓ Marketing Policy 2026
✓ Approval SOP
□ Regulatory Guidelines
□ HR Policy
                              [Apply]
```

- **Current document:** retrieval constrained to the one open document (only available when Ask AI was launched from a Document Workspace).
- **Selected documents:** retrieval constrained to a user-picked set via `DocumentSelector` (checkbox list with search/filter, same component used in Comparison's document pickers).
- **Entire knowledge base:** retrieval spans everything the user has access to; UI shows a subtle "(may take slightly longer)" hint since retrieval fans out wider.

Changing scope **mid-conversation** is allowed but shown as a visible system marker in the transcript ("Scope changed to: Entire knowledge base") so prior answers aren't misread as having used the new scope.

**Components:** `ConversationList`, `ScopeSelector`, `DocumentSelector`, `ChatWindow`, `ChatMessage`, `ChatInput`, `SuggestedQuestions`, `StreamingIndicator`, `FeedbackButtons`, `RegenerateButton`, `CopyButton`.

**Interactions:**
- Streaming token-by-token response render (see [§11](#11-real-time-processing-ux)); a stop-generating control appears while streaming.
- Citations render inline as numbered badges ([§6.7](#67-ai-answer--citation-experience) / [§12](#12-citation-ux-deep-dive)).
- "Regenerate" re-runs the same question (optionally with a "try again with different documents" affordance if the answer was weak).
- "Copy" copies the answer text with citation markers preserved as a reference list appended.
- Feedback (👍/👎) captures lightweight signal; 👎 opens an optional one-line reason field.
- Suggested follow-up questions are generated from the current answer's content and document scope; clicking one submits it immediately.
- Message input supports `@` mention to quickly add a document to scope without leaving the input.

**Data required:** conversation list (paginated), message history for active conversation, document list for scope picker, streaming answer tokens, citation objects per message.

**API dependencies:** `GET /chat/conversations`, `GET /chat/conversations/:id`, `POST /chat/conversations` (new), `POST /chat/conversations/:id/messages` (SSE/streamed response), `POST /chat/messages/:id/feedback`, `GET /documents?forSelector=true`.

**UX States:**
- **Loading (conversation list):** skeleton rows.
- **Loading (message send, pre-stream):** `StreamingIndicator` — "Searching documents…" → "Reading 6 sources…" → token stream begins. This staged loading label matters: it tells the user retrieval is actually happening, not just "thinking."
- **Streaming:** partial text renders live; citation badges only attach once their reference is fully resolved (avoid flickering half-built citations).
- **Empty (no conversations yet):** centered prompt with example questions relevant to the org's most common document types.
- **Empty scope (0 documents selected under "Selected documents"):** input is disabled with inline guidance: "Select at least one document to ask a question."
- **Error (generation failed):** an inline error message bubble in place of the assistant turn, with Retry — the user's question is preserved, not lost.
- **No grounded answer found:** the model must explicitly say so in-UI (a distinct visual treatment — muted background, no citation badges, an icon indicating "no supporting source found") rather than presenting an ungrounded answer identically to a grounded one. This is a core trust mechanism, not a copy nuance.
- **Partial answer (some sources unavailable, e.g., a document mid-processing):** inline notice above the answer: "This answer excludes 1 document still processing."

**Responsive:** Conversation list collapses into a drawer below `md`; chat transcript becomes full-width. Scope selector collapses into a compact chip ("Scope: 2 documents ▾") that expands a bottom sheet on mobile rather than an inline panel.

**Accessibility:** new message announced via `aria-live="polite"` region distinct from the streaming text itself (to avoid a screen reader reading every token); input is a `textarea` with `Enter` to send / `Shift+Enter` for newline, documented in a keyboard-shortcut hint; citation badges are focusable buttons with descriptive `aria-label`s ("Citation 1: Marketing Policy 2026, page 12").

---

### 6.7 AI Answer + Citation Experience

**Purpose:** Make every factual claim in an AI answer traceable to exact source evidence — the product's core trust mechanism.

**Anatomy of an answer:**

```text
The approval process contains four stages. [1][2]

Sources
[1] Marketing Policy 2026
    Page 12 · Section 4.2 · Effective Jan 2026
[2] Approval SOP
    Page 8 · Section 3
```

**Citation badge states:**

| State | Appearance | Trigger |
|---|---|---|
| Default | Small numbered pill, subtle border | Rendered inline after streaming completes for that claim |
| Hover | Elevated, shows `SourcePreview` popover | Mouse hover / keyboard focus |
| Active/selected | Filled accent color | Clicked — corresponding source is open in the side panel/viewer |
| Unresolved | Dashed border, muted | Citation reference exists but source lookup failed (rare, logged) |

**Hover behavior — `SourcePreview` popover:**

```text
┌──────────────────────────────────────────┐
│ Marketing Policy 2026 · p.12 · §4.2       │
│ "...the approval process requires four   │
│  sequential stages: submission, manager  │
│  review, compliance review, and final    │
│  sign-off..."                             │
│                          [Open source →] │
└──────────────────────────────────────────┘
```
The popover shows the **exact source span** (highlighted within surrounding context, ~2–3 sentences), not just the citation location — the user should be able to verify the claim without leaving the chat.

**Click behavior:**
1. Clicking a citation badge (or "Open source") opens/updates the **Source panel** (in the Three-Panel workspace) or navigates into the Document Workspace viewer if in a single-panel chat context.
2. The target document opens to the exact page.
3. The exact source-text span is highlighted (persistent highlight, not just scroll-into-view) using the same highlight-overlay mechanism as [§13](#13-document-viewer-ux-deep-dive).
4. If the citation's document is not the currently open one, a confirmation is unnecessary — it simply switches, with a breadcrumb-style "back to chat" affordance preserved.

**Components:** `CitationBadge`, `SourcePreview` (popover), `CitationList` (the "Sources" block under a message), `SourceHighlight` (viewer overlay).

**Interactions:** keyboard-focusable badges (`Tab` cycles through citations in a message); `Enter`/`Space` opens the source; hover-preview also triggerable via focus for keyboard users (not mouse-only).

**Data required:** per-citation: document ID, version ID, page number, section path, exact text span (start/end offsets or bounding box), confidence/relevance score (internal, not always user-facing).

**API dependencies:** citations are returned embedded in the chat message payload (`GET/POST /chat/conversations/:id/messages` response includes a `citations[]` array); `GET /documents/:id/content?version=&page=` used to fetch the page for source panel rendering if not already cached.

**UX States:**
- **Loading source preview:** small inline spinner inside the popover if the source text needs a fetch (usually pre-fetched with the message).
- **No citation available for a claim:** the claim is visually flagged (see [§6.6](#66-ai-chat--ask-ai) "No grounded answer found") rather than silently presented as if cited.
- **Citation source unavailable** (document deleted/access revoked since generation): badge shows a muted/disabled state with tooltip "Source no longer accessible."
- **Multiple citations, same claim:** badges group and are individually clickable (`[1][2]`), not merged into one.

**Responsive:** Popover becomes a bottom-sheet on mobile/touch (hover doesn't exist); tap opens the sheet with the same content plus an explicit "Open source" button.

**Accessibility:** `aria-describedby` links the badge to its popover content; popovers are dismissible via `Escape` and don't trap focus; the Sources list under each message is also fully navigable independent of the inline badges (belt-and-suspenders for screen reader users who prefer list navigation over inline reading).

---

### 6.8 Three-Panel Research Workspace

**Purpose:** The advanced, power-user surface for deep research — browse documents, converse with AI, and inspect exact evidence, all without losing context by navigating away.

**Layout:**

```text
┌────────────┬──────────────────────┬──────────────────────┐
│ Documents  │ AI Answer            │ Source Evidence       │
│ (18% min)  │ (flex, 44% default)  │ (38% default)         │
│            │                       │                       │
│ ☑ Policy   │ Q: What changed in   │ Marketing Policy 2026│
│ ☑ SOP      │ the approval flow?   │ Page 12 · §4.2        │
│ ☐ Contract │                       │                       │
│            │ A: The approval win- │ "...must be completed│
│            │ dow was extended     │ within 7 business    │
│            │ from 5 to 7 business │ days, an increase    │
│            │ days. [1]             │ from the prior 5-day │
│            │                       │ requirement..."       │
│            │ [1] [2]               │        [highlighted] │
└────────────┴──────────────────────┴──────────────────────┘
```

**Panel roles:**
- **Left — Documents:** the active scope selector (identical semantics to [§6.6](#66-ai-chat--ask-ai)'s scope picker, always visible, not a modal) plus a lightweight document list for quick scope toggling mid-session.
- **Center — AI Answer:** the chat transcript for this research session (reuses `ChatWindow`/`ChatMessage`).
- **Right — Source Evidence:** synced to whichever citation is currently active; shows the rendered page with highlight, plus section/page navigation controls and an "Open full document" escape hatch to the full Document Workspace.

**Panel behavior:**
- **Resizing:** all three panels are resizable via drag handles between them; min-widths enforced (Documents 220px, Answer 360px, Evidence 320px) to prevent unusable collapse. Panel widths persist per-user in `localStorage`.
- **Collapsing:** Documents and Evidence panels can be individually collapsed to icon rails (click to expand) when the user wants to focus on the answer; a collapsed rail still shows a badge (e.g., active document count, or "3 sources" for evidence).
- **Synchronization:** clicking any citation in the center panel updates the right panel immediately (no navigation, no modal) — this synchronous three-way relationship is the entire point of this screen. Selecting a different document in the left panel does **not** clear the transcript; it only changes future-turn retrieval scope (mirrors [§6.6](#66-ai-chat--ask-ai) mid-conversation scope-change behavior).

**Components:** reuses `DocumentSelector`, `ChatWindow`, `ChatMessage`, `CitationBadge`, `SourcePreview`'s content but inline (not popover) inside `SourcePanel`, plus a new `ResizablePanelGroup` layout primitive and `PanelCollapseToggle`.

**Interactions:** drag-to-resize with a visible handle on hover; double-click a handle resets to default split; `Evidence` panel has its own mini page-navigation (`‹ Page 12 of 40 ›`) independent of the Document Workspace's main viewer, since this is a focused excerpt view, not the full reading experience.

**Data required:** same as Ask AI (conversation, messages, citations) plus the currently "focused" citation/source state, which is client-only UI state (not persisted server-side beyond the conversation itself).

**API dependencies:** same as [§6.6](#66-ai-chat--ask-ai); source panel content fetched via `GET /documents/:id/content?version=&page=` on citation focus (cached per document+page for the session).

**UX States:**
- **Loading evidence:** right panel shows a page skeleton while fetching.
- **Empty evidence (no citation focused yet):** right panel shows a neutral prompt — "Select a citation to view its source" — rather than a blank pane.
- **Error (evidence fetch fails):** inline retry within the right panel only; doesn't disrupt the chat.
- All chat-related states from [§6.6](#66-ai-chat--ask-ai) apply to the center panel.

**Responsive:** **Desktop-first — this screen requires ≥1280px to render as three panels.** Below `lg`, it degrades to a **two-panel view** (Documents panel collapses to a top scope-chip bar; Answer + Evidence become tabs the user switches between, with a badge on "Evidence" when a new citation is focused while the user is on the Answer tab). Below `md`, the screen is not offered as a distinct route — mobile users are guided to standard Ask AI ([§6.6](#66-ai-chat--ask-ai)), which links out to full document view for evidence instead of an inline synced panel.

**Accessibility:** resizable panels are operable via keyboard (focus handle, arrow keys to resize, documented in a shortcut hint); panel collapse toggles are labeled buttons; synchronization updates the Evidence panel's live region so screen reader users know it changed ("Source updated: Marketing Policy 2026, page 12").

---

### 6.9 Search

**Purpose:** A dedicated, precise way to query the knowledge base directly — for users who want to browse evidence themselves rather than receive a synthesized answer.

**Layout:**

```text
┌───────────────────────────────────────────────────────────────┐
│ [ Search the knowledge base…             ] [Semantic▾][Search]│
│ Filters: Type ▾  Department ▾  Collection ▾  Date ▾           │
├───────────────────────────────────────────────────────────────┤
│ 24 results for "approval process"                              │
│                                                                  │
│ Marketing Policy 2026                          Relevance: 94% │
│ Section 4.2 — Regulatory Review                                │
│ "...approval process requires regulatory review before…"      │
│ Page 12                                          [Open][Ask AI]│
│ ──────────────────────────────────────────────────────────── │
│ Approval SOP                                    Relevance: 88%│
│ Section 3 — Stage Definitions                                  │
│ "...the approval process contains four sequential stages…"    │
│ Page 8                                           [Open][Ask AI]│
└───────────────────────────────────────────────────────────────┘
```

**Search mode toggle:** Hybrid (default) / Semantic / Keyword — exposed as a compact dropdown next to the search bar, not three separate tabs, since Hybrid is correct for 95% of use. Mode choice is a power-user override, kept accessible but not visually competing with the primary search action.

**Components:** `SearchBar`, `SearchModeSelector`, `FilterBar` (shared with Documents page filter primitives), `SearchResultCard`, `RelevanceIndicator`, `SearchResultSnippet` (with query-term highlighting), `Pagination`.

**Interactions:**
- Results update on submit (Enter or Search button), not on every keystroke (avoid excessive query load); a debounced instant-preview of top 3 results may appear below the bar while typing, fully replaced by the real result list on submit.
- Each result card: document name (→ Document Workspace), section/page context, matched snippet with the query terms bolded, relevance score, and two actions — **Open** (jumps into the document viewer at that page with the passage highlighted, same mechanism as citation click) and **Ask AI** (starts a new Ask AI conversation scoped to "Current document" with this passage's context pre-attached).
- Filters identical in spirit to the Documents page (type, department, collection, date range) but scoped to searchable content, plus a version-awareness filter ("Current versions only" toggle, default on, since users usually don't want to search superseded content).
- Result grouping: results are shown flat by relevance by default; a "Group by document" toggle re-clusters multiple matching passages under their parent document.

**Data required:** query string, mode, filters, paginated results with snippet + relevance + location metadata.

**API dependencies:** `GET /search?query=&mode=hybrid&type=&department=&collection=&currentOnly=&page=`.

**UX States:**
- **Loading:** skeleton result cards (5) below the search bar; search bar shows a subtle spinner.
- **Empty (no query yet):** helpful landing state — recent searches (org-level popular queries or the user's own recent searches) and example queries.
- **No results:** "No results for '…'" + suggestions (broaden filters, switch to Semantic mode, check spelling) — never a bare blank state.
- **Error:** inline banner with Retry, search bar remains usable.
- **Partial (some collections unindexed/processing):** a dismissible notice: "2 documents are still processing and excluded from results."

**Responsive:** Fully responsive — search is a first-class mobile use case (quick lookup). Filter bar collapses to a "Filters" sheet on mobile; result cards stack full-width with the same content, snippet truncated to 2 lines with "…more."

**Accessibility:** search input has a persistent visible label (not placeholder-only for the label semantics, though placeholder text can supplement); result count announced via `aria-live="polite"` after search completes; relevance score has a text equivalent (not conveyed by color/bar alone — "Relevance: 94%" as text, with the bar as reinforcement).

---

### 6.10 Document Comparison

**Purpose:** Precisely surface what changed between two document versions (or two related documents), at both a summary and clause level, with every change traceable to source.

**Layout — Step 1, selection:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Compare Documents                                               │
│                                                                   │
│ Document A                    Document B                        │
│ [ Marketing Policy 2025  ▾]   [ Marketing Policy 2026  ▾]      │
│                                                                   │
│                      [ Compare → ]                              │
└───────────────────────────────────────────────────────────────┘
```

`DocumentSelector` supports comparing two versions of the *same* document (most common — pre-filtered "suggest other versions" once Document A is picked) or two *different* documents (e.g., comparing a vendor's proposed contract against the org's standard template).

**Layout — Step 2, results:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Marketing Policy 2025  ↔  Marketing Policy 2026                │
│ 12 changes detected      4 Major · 5 Moderate · 3 Minor         │
│ [Summary] [Side-by-side] [Changes list]        [View: Split▾]  │
├───────────────────┬───────────────────────────────────────────┤
│ Sections           │  2025                  │  2026             │
│ ▸ 1. Purpose        │  Approval must be     │  Approval must be│
│ ▸ 2. Scope   [~]     │  completed within 5   │  completed within│
│ ▾ 3. Approval [~~]   │  business days.       │  7 business days.│
│    3.1 Timeline [M]  │                       │  (highlighted:   │
│ ▸ 4. Records         │                       │   modified)      │
└───────────────────┴───────────────────────────────────────────┘
```

**Components:** `ComparisonSelector`, `ComparisonSummary` (severity breakdown), `DiffViewer` (side-by-side or unified), `SectionNavigator` (mirrors the change-density markers seen in the sidebar: `[~]` some changes below, `[M]` major change on this node), `ChangeCard`, `ChangeSeverityBadge`.

**Visual encoding of diff states:**

| State | Treatment |
|---|---|
| **Added** | Green left-border block, `+` gutter marker, light green background wash |
| **Removed** | Red left-border block, `−` gutter marker, light red background wash, strikethrough text |
| **Modified** | Amber left-border block, `~` gutter marker; the specific changed span within the sentence is bolded/underlined against the unchanged surrounding text (word-level diff, not just paragraph-level flagging) |
| **Unchanged** | No special treatment — plain text, slightly recessed/lower-contrast relative to changed blocks so changes pop visually |

**Comparison summary card:**

```text
12 changes detected

■■■■ 4 Major       (e.g., approval timelines, liability terms)
■■■■■ 5 Moderate   (e.g., role name changes, added clauses)
■■■ 3 Minor        (e.g., formatting, wording)
```
Severity is computed server-side (based on section criticality + magnitude of semantic change) and is user-facing as a filter (checkbox per severity tier above the diff view) so users can triage Major changes first.

**View modes:**
- **Side-by-side (default on desktop):** two synchronized-scroll columns.
- **Unified:** single column, inline added/removed/modified markup — better for narrow viewports.
- **Changes list:** a flat, filterable list of `ChangeCard`s only (no full document body shown) — fastest way to review just the deltas.

**Interactions:**
- Section navigator lets the user jump directly to sections containing changes, with density markers so they can prioritize.
- Clicking any diff block opens a `ChangeCard` detail (or expands inline) showing old/new value, severity, and **"View Sources"** which deep-links into both documents at the exact page/section (opens the Source panel or navigates to Document Workspace, consistent with citation click behavior in [§6.7](#67-ai-answer--citation-experience)).
- Synchronized scrolling between the two columns can be toggled off for users who want to review independently.
- "Ask AI about this comparison" action available at the top — starts a conversation scoped to both documents, useful for "why did this change?" style follow-ups (if the org has change-rationale metadata) or cross-document synthesis.

**Data required:** two document/version identifiers, structured diff result (list of changes with type/severity/section/page/old-text/new-text/citations), section tree for both versions.

**API dependencies:** `POST /comparison` (`{documentAId, versionA, documentBId, versionB}` → `comparisonId`), `GET /comparison/:id`, `GET /comparison/:id/changes?severity=&section=`.

**UX States:**
- **Loading (running comparison):** this is a potentially slow, compute-heavy operation — shown as a dedicated processing state, not a spinner: "Comparing documents… analyzing 40 pages" with an indeterminate-but-staged progress (Extracting differences → Classifying severity → Generating summary), streamed via the same job-progress mechanism as document processing where feasible.
- **Empty (0 changes detected):** a clear positive-empty state: "No differences detected between these versions" — explicitly reassuring, not an ambiguous blank screen.
- **Error:** if one document isn't `READY` yet, comparison is blocked at Step 1 with an inline explanation ("Marketing Policy 2026 is still processing") rather than allowed to fail after submission.
- **Partial (large documents, comparison truncated/sampled):** disclosed explicitly if the backend limits scope ("Comparison covers the first 200 pages of each document").

**Responsive:** **Desktop-first.** Side-by-side view requires ≥1024px; below that, the view auto-switches to Unified mode (not user-toggleable at that width, since side-by-side would be unusable). Changes-list mode remains fully usable down to mobile widths and is the default view on small screens.

**Accessibility:** diff color coding is always paired with the gutter symbol (`+`/`−`/`~`) and a text label in `ChangeCard`s ("Modified") — never color-only; synchronized scroll can be disabled for users who find linked scrolling disorienting (also aids screen reader / switch-device users); section navigator is a proper tree with `aria-expanded`.

---

### 6.11 Change Detection

**Purpose:** The atomic unit of comparison output — a single, inspectable change with full context and provenance.

**Layout (`ChangeCard`, used both inline in the Comparison view and standalone in lists/reports):**

```text
┌─────────────────────────────────────────────────┐
│ ● Modified — Approval Timeline        [Moderate]│
│ Section 3.1 · Approval Process                    │
│                                                     │
│ 2025                                               │
│ "Approval must be completed within 5 business     │
│  days."                                            │
│              ↓                                     │
│ 2026                                               │
│ "Approval must be completed within 7 business     │
│  days."                                            │
│                                                     │
│ [View Sources]              [Open in document →] │
└─────────────────────────────────────────────────┘
```

**Fields:**
- **Change category:** Added / Removed / Modified (icon + label, top-left, colored per [§6.10](#610-document-comparison) encoding).
- **Severity badge:** Major (red) / Moderate (amber) / Minor (gray-blue), top-right.
- **Section context:** breadcrumb path to the change's location.
- **Old value / New value:** rendered as quoted source text (word-level diff highlighting applied within, for Modified type). For Added, only "New" is shown (no "Old" block); for Removed, only "Old."
- **Source citations:** "View Sources" expands (or links to) the exact citation objects for both the old and new text, reusing `CitationBadge`/`SourcePreview`.
- **Navigation:** "Open in document" routes into the Document Workspace at the correct version/page/highlight.

**Interactions:** cards are collapsible (collapsed = one-line summary "Modified — Approval Timeline (Moderate)"); expand/collapse state can be bulk-toggled ("Expand all" / "Collapse all") in the parent Comparison changes-list view.

**Data required:** change type, severity, section path, old/new text spans, citation refs for each side.

**API dependencies:** part of `GET /comparison/:id/changes` payload; no separate endpoint needed unless deep-linked (`GET /comparison/:id/changes/:changeId` for shareable links).

**UX States:** Loading (skeleton card), Error (if citation resolution fails, card still renders with old/new text but "View Sources" shows a disabled/tooltip state), and the standard collapsed/expanded interaction states.

**Responsive:** Card reflows old/new from a vertical stack (mobile default) to side-by-side columns at `md`+ if space allows within its container.

**Accessibility:** severity conveyed with icon + text, not color alone; the "↓" transition between old/new has an `aria-label` ("changed to") for screen readers rather than relying on visual arrow alone.

---

### 6.12 Document Summary

**Purpose:** A fast, trustworthy digest of a document's content for users who need the gist before (or instead of) reading the full text — every claim still sourced.

**Layout:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Summary — Marketing Policy 2026 (v2.1)          [Regenerate]  │
├───────────────────────────────────────────────────────────────┤
│ Executive Summary                                                │
│ This policy defines the marketing approval workflow, requiring  │
│ four sequential review stages before external publication. [1] │
│                                                                   │
│ Key Points                                                       │
│ • Approval window extended to 7 business days [2]                │
│ • Compliance review is mandatory for all external content [3]   │
│                                                                   │
│ Important Dates          Roles                                  │
│ • Effective: Jan 1 2026  • Approver: Marketing Director [4]     │
│                            • Reviewer: Compliance Team [5]       │
│                                                                   │
│ Requirements              Risks                                  │
│ • All content must be…    • Non-compliance may delay launch [6]│
│                                                                   │
│ Topics: approval-process, compliance, branding, timelines       │
└───────────────────────────────────────────────────────────────┘
```

**Components:** `SummarySection` (reusable for each labeled block: Executive Summary, Key Points, Important Dates, Roles, Requirements, Risks), `TopicTagList`, `CitationBadge` (every bullet/sentence with a factual claim carries one), `RegenerateButton`.

**Interactions:** each block's citations behave identically to Ask AI citations ([§6.7](#67-ai-answer--citation-experience)) — hover preview, click opens source panel/document at exact location. "Regenerate" re-runs summarization (useful after a document is re-processed or if the user wants a refreshed take); a subtitle shows generation timestamp and model/version used for auditability.

**Data required:** structured summary object (`executiveSummary`, `keyPoints[]`, `dates[]`, `roles[]`, `requirements[]`, `risks[]`, `topics[]`), each list item paired with citation(s).

**API dependencies:** `GET /summaries/:documentId?version=`, `POST /summaries/:documentId/regenerate`.

**UX States:**
- **Loading:** skeleton blocks matching the section layout (not a single spinner) so the structure is visible immediately.
- **Generating (regenerate in progress):** existing summary stays visible, dimmed, with a "Regenerating…" banner — never blank the screen while waiting.
- **Empty (a section has no content, e.g., no dates found):** section explicitly states "No dates identified in this document" rather than being omitted — omission would be ambiguous (did it not run, or truly find nothing?).
- **Error:** "Couldn't generate summary" with Retry; if a stale summary exists, it remains visible with a warning banner rather than being replaced by an error block.
- **Partial (document too large, summary covers a subset):** disclosed inline: "Summary based on the first 150 pages."

**Responsive:** Two-column blocks (Dates/Roles, Requirements/Risks) stack to single column below `md`.

**Accessibility:** section headings are real `<h2>`/`<h3>` elements for screen-reader navigation (landmark jumping); topic tags are a labeled list, not just visual chips.

---

### 6.13 Conflict Detection

**Purpose:** Surface contradictions between documents (or versions) proactively, since conflicting policy/procedure text is a compliance risk the user may not think to search for.

**Layout (`ConflictCard`, appears in a dedicated Conflicts view — reachable from Dashboard alerts, Document Workspace, and Analytics — and inline wherever relevant, e.g., flagged in Ask AI answers when retrieved sources disagree):**

```text
┌─────────────────────────────────────────────────┐
│ ⚠ Potential Conflict                    [Major] │
│ Approval Timeline Requirement                     │
│                                                     │
│ Document A — Marketing Policy 2026 (effective     │
│ Jan 2026)                                          │
│ "Approval must happen within 7 days."       [1]  │
│                                                     │
│ Document B — Regional Marketing Addendum           │
│ (effective Mar 2025, not superseded)                │
│ "Approval must happen within 5 days."       [2]  │
│                                                     │
│ [Compare Sources]        [Mark as Reviewed ▾]     │
└─────────────────────────────────────────────────┘
```

**Fields:**
- **Severity:** Major / Moderate / Minor — based on how central the conflicting statement is (e.g., a compliance deadline vs. a stylistic inconsistency).
- **Conflicting documents:** each side shows document name, **effective date**, and superseded status — this is critical, since an apparent conflict is often just an outdated document that should have been archived; the UI must make effective-date context immediately visible, not buried.
- **Source citations:** each side's statement is a real citation, clickable like any other.
- **Resolution workflow:** "Mark as Reviewed" dropdown → "Not a conflict (explain why)" / "Escalate to owner" / "Superseded — archive Document B" (permission-gated to Editor/Admin roles). Resolved conflicts move to a "Reviewed" tab with resolver name + timestamp, but remain visible/auditable rather than deleted.

**Effective-date awareness:** the system computes conflicts primarily between documents whose effective periods overlap; the UI visually de-emphasizes (but doesn't hide) conflicts against clearly superseded documents, with a "Likely resolved by version update" hint distinguishing genuine open conflicts from stale-document artifacts.

**Interactions:** "Compare Sources" opens the full Comparison view pre-loaded with both documents, section-anchored to the conflicting passage — reuses [§6.10](#610-document-comparison) entirely rather than duplicating diff UI.

**Data required:** conflict object (severity, statement pair, each with document/version/effective-date/citation), resolution status/history.

**API dependencies:** `GET /conflicts?status=open`, `GET /conflicts/:id`, `POST /conflicts/:id/resolve`.

**UX States:**
- **Loading:** skeleton conflict cards.
- **Empty (no open conflicts):** positive empty state — "No conflicts detected across your knowledge base" with a subtle timestamp of last conflict-scan run.
- **Error:** inline retry; doesn't block other Dashboard/Analytics widgets.
- **Permission denied:** "Mark as Reviewed" hidden/disabled for Viewer-role users, visible read-only otherwise.
- **Partial (conflict scan incomplete — new documents not yet analyzed):** banner: "3 recently uploaded documents haven't been scanned for conflicts yet."

**Responsive:** Card layout matches `ChangeCard` responsive behavior (stack old/new vertically on mobile).

**Accessibility:** the `⚠` icon always paired with the text "Potential Conflict" (never icon-only); resolution dropdown is a proper accessible menu (`role="menu"`, keyboard arrow navigation).

---

### 6.14 Analytics

**Purpose:** Give admins/ops visibility into system health, usage, and AI answer quality — operational trust, not just end-user trust.

**Layout:**

```text
┌───────────────────────────────────────────────────────────────┐
│ Analytics                                    Range: Last 30d ▾│
├───────────┬───────────┬───────────┬───────────┬───────────────┤
│ Documents │ Questions │ Avg Resp. │ Citation  │ Grounded       │
│  1,284    │  8,421    │  2.7 sec  │ Coverage  │ Answers        │
│           │           │           │  94%      │  92%           │
├───────────┴───────────┴───────────┴───────────┴───────────────┤
│ Response Time Breakdown         │  Failed Queries    3%        │
│ [chart: retrieval vs LLM ms]    │  [list of top failure reasons]│
├───────────────────────────────────────────────────────────────┤
│ Processing Jobs (30d)            │  Token / Cost Usage          │
│ [chart: completed vs failed]     │  [chart: tokens by day/model]│
└───────────────────────────────────────────────────────────────┘
```

**Metrics covered:**

| Metric | Definition |
|---|---|
| Documents | Total ingested, trend vs. prior period |
| Questions | Total Ask AI queries |
| Avg Response | End-to-end latency, user-perceived |
| Retrieval latency | Search/vector-lookup portion of response time |
| LLM latency | Generation portion of response time |
| Citation coverage | % of answer sentences with ≥1 citation |
| Grounded answer % | % of answers where all claims trace to retrieved sources (vs. flagged as ungrounded) |
| Failed queries | % of queries erroring or returning no usable answer |
| Processing jobs | Ingestion pipeline throughput/failure rate |
| Token/cost | LLM token consumption, broken down by model, with estimated cost |

**Components:** `KpiCard`, `TimeRangeSelector`, `LineChart`/`BarChart` (thin wrapper around a charting lib, e.g., Recharts), `LatencyBreakdownChart`, `FailureReasonList`, `CostBreakdownTable`.

**Interactions:** every KPI/chart supports the same global time-range selector; charts are hoverable for exact values (tooltip), and clicking a "Failed Queries" entry deep-links to that query's detail (if logged) for debugging. Export (CSV) available for admin/finance use (token/cost data).

**Data required:** aggregated metrics for the selected time range, comparison to prior period (trend arrows), breakdowns by day/model/document-type as applicable.

**API dependencies:** `GET /analytics/summary?range=`, `GET /analytics/latency?range=`, `GET /analytics/quality?range=` (citation coverage, grounded %), `GET /analytics/processing?range=`, `GET /analytics/usage?range=` (tokens/cost).

**UX States:**
- **Loading:** skeleton KPI cards + chart placeholders.
- **Empty (new org, insufficient data):** "Not enough activity yet to show analytics" rather than zeroed/broken charts.
- **Error:** per-widget retry, consistent with Dashboard pattern.
- **Access-gated:** Analytics is Admin/Owner-role only by default; other roles see a permission-denied state or the nav item is hidden entirely per org policy (configurable in Settings → Roles).

**Responsive:** Charts reflow to full-width single-column stack below `md`; complex charts (latency breakdown) switch to a simplified summary-stat view on mobile rather than cramming an unreadable chart into a small viewport — full chart remains available via "View full report" on desktop.

**Accessibility:** every chart has a text-table equivalent available via a "View as table" toggle (critical since charts are not reliably screen-reader accessible); KPI trend arrows include text ("+12% vs previous period"), not arrow-icon-only.

---

### 6.15 Settings

**Purpose:** Centralized administration for identity, org configuration, AI behavior, and governance.

**Layout (left sub-nav within the Settings section):**

```text
┌───────────────┬───────────────────────────────────────────────┐
│ Profile        │  [Selected settings panel content]            │
│ Organization   │                                                 │
│ Users          │                                                 │
│ Roles          │                                                 │
│ AI Settings    │                                                 │
│ Documents      │                                                 │
│ Integrations   │                                                 │
│ Security       │                                                 │
│ Audit Log      │                                                 │
└───────────────┴───────────────────────────────────────────────┘
```

**Sub-sections:**

| Section | Contents |
|---|---|
| **Profile** | Name, email, avatar, password change, notification preferences |
| **Organization** | Org name/logo, default timezone, storage usage, billing plan summary |
| **Users** | Invite/remove users, assign roles, pending invitations, `UserTable` with search/filter |
| **Roles** | Role definitions (Admin/Editor/Viewer, or custom RBAC if supported) with permission matrix editor |
| **AI Settings** | Default retrieval mode (hybrid/semantic/keyword), citation strictness threshold, allowed LLM model tier, response language, "require citation for every claim" toggle |
| **Document Settings** | Default access level for new uploads, allowed file types/size limits, retention policy, auto-archival rules for superseded versions |
| **Integrations** | SSO/SAML config, storage connectors (SharePoint/S3), webhook endpoints, API keys |
| **Security** | 2FA enforcement, session timeout policy, IP allowlist, SSO-only enforcement toggle |
| **Audit Log** | Searchable/filterable log of sensitive actions (permission changes, deletions, exports, access-level changes) with actor/timestamp/target |

**Components:** `SettingsNav`, `SettingsPanel`, `UserTable`, `RolePermissionMatrix`, `ToggleSetting`, `ApiKeyManager`, `AuditLogTable`, `InviteUserModal`.

**Interactions:** most settings save via explicit "Save changes" (not silent auto-save) for anything security/permission-related, with a confirmation for destructive-adjacent changes (e.g., "Enforce SSO-only will sign out all password-based sessions — continue?"); toggle-style preferences (e.g., notification prefs) may auto-save with a toast confirmation.

**Data required:** current org config, user list with roles, role/permission matrix, audit log entries (paginated), integration connection statuses.

**API dependencies:** `GET/PUT /settings/profile`, `GET/PUT /settings/organization`, `GET/POST/DELETE /users`, `GET/PUT /settings/roles`, `GET/PUT /settings/ai`, `GET/PUT /settings/documents`, `GET/POST/DELETE /settings/integrations`, `GET/PUT /settings/security`, `GET /settings/audit-log?query=&actor=&range=`.

**UX States:**
- **Loading:** skeleton form fields / table rows per panel.
- **Saving:** button shows spinner + disabled; form fields locked during save.
- **Success:** inline confirmation banner or toast, scoped to the panel ("Organization settings saved").
- **Error (validation):** field-level errors; **error (save failed):** banner at top of panel, form state preserved (never lose unsaved input).
- **Permission denied:** entire sub-sections (e.g., Security, Integrations) hidden or shown read-only for non-Admin roles, with a tooltip explaining the restriction rather than a dead click.
- **Empty (Users):** "No users yet besides you — invite your team" CTA.
- **Empty (Audit Log, no matches):** "No audit events match your filters."

**Responsive:** Sub-nav collapses into a dropdown selector above the panel content below `md`; tables (Users, Audit Log) follow the same card-collapse pattern as the Documents table ([§6.3](#63-documents-page)).

**Accessibility:** settings forms use `fieldset`/`legend` grouping for related controls; the permission matrix is an accessible grid (`role="grid"`, arrow-key navigable) rather than a bare table of checkboxes; destructive-adjacent confirmations use accessible modal dialogs (see [§16](#16-accessibility)).

---

## 7. User Journeys

### Journey 1 — Upload → Processing → Ready

1. User clicks **Upload** (Dashboard, header, or Documents page).
2. Drags in 1–3 files; sets Collection + Access; confirms.
3. Files upload in parallel with byte-progress; each transitions to the step tracker on completion.
4. User closes the modal — global header processing indicator continues tracking.
5. Live status updates arrive via SSE/WebSocket; Documents table row for each file updates its status badge in place (no manual refresh).
6. On `READY`, a toast offers "View document"; on `FAILED`, the row shows the failure reason with Retry.
7. **Success criterion:** user can locate and open a newly uploaded document without needing to refresh or search for it — it's visibly the top row in "Recently Uploaded."

### Journey 2 — Open Document → Ask Question → Receive Answer → Inspect Citation

1. User opens a document from the Documents table → Document Workspace.
2. Clicks **Ask AI** in the document header → Ask AI opens with scope pre-set to "Current document."
3. Types a question; streaming answer appears with inline citation badges.
4. User hovers `[1]` → `SourcePreview` popover shows the exact source sentence.
5. User clicks `[1]` → document viewer (or Source panel, if in the three-panel context) jumps to the exact page with the passage highlighted.
6. **Success criterion:** from question to verified source takes ≤2 clicks and zero manual searching.

### Journey 3 — Select Multiple Documents → Cross-Document Question

1. User opens Ask AI (or Research Workspace) and sets scope to "Selected documents."
2. Uses `DocumentSelector` to check 2–4 relevant documents (e.g., Policy + SOP + Regional Addendum).
3. Asks a synthesis question ("What's the full approval process end to end?").
4. Answer streams with citations spanning multiple documents, each badge indicating its source document distinctly (badge tooltip includes doc name, not just number).
5. If retrieved sources disagree, the answer surfaces a conflict notice inline (linking to [§6.13](#613-conflict-detection) detail) rather than silently picking one.
6. **Success criterion:** user can tell, at a glance, which parts of the answer came from which document.

### Journey 4 — Compare 2025 vs 2026 → Inspect Changes → Open Source

1. User navigates to **Compare**, selects Document A (2025) and Document B (2026) — or launches directly from a document's **Compare** action.
2. Comparison runs (staged processing indicator); results land on the Summary view (12 changes, severity breakdown).
3. User filters to "Major" severity, switches to Changes List view.
4. Expands the "Approval Timeline" `ChangeCard`, reviews old/new text.
5. Clicks **View Sources** → both citations open (2025 source and 2026 source) for direct verification.
6. Clicks **Open in document** → lands in the 2026 Document Workspace at the exact page/section, highlighted.
7. **Success criterion:** user can defend "why does this show as a Major change" by pointing at real text in both documents.

### Journey 5 — Search Knowledge Base → Open Result → Inspect Source

1. User types a query into **Search**, reviews snippet-based results with relevance scores.
2. Clicks **Open** on a result → Document Workspace opens at the matched page, passage highlighted (same highlight mechanism as citations).
3. Alternatively clicks **Ask AI** on a result to get a synthesized answer scoped to that document, carrying the search context forward.
4. **Success criterion:** search and Ask AI feel like two entry points into the same underlying evidence, not two disconnected tools.

### Journey 6 — Detect Conflict → Compare Conflicting Sources

1. User sees a conflict flagged (Dashboard alert, Document Workspace section marker `[•]`, or surfaced inline during an Ask AI answer).
2. Opens the `ConflictCard` — reviews both statements with effective dates and severity.
3. Clicks **Compare Sources** → full Comparison view opens, anchored to the conflicting section in both documents.
4. Determines resolution (e.g., Document B is outdated) and, if authorized, marks the conflict Reviewed with a resolution note.
5. **Success criterion:** the user never has to manually locate the conflicting passage in either source document — it's handed to them pre-navigated.

---

## 8. Component Architecture

### 8.1 Folder Structure

```text
src/
├── app/                     # routing, app shell, providers
├── components/
│   ├── layout/              # Sidebar, Header, AppShell, CommandPalette
│   ├── documents/           # DocumentTable, DocumentCard, DocumentHeader, DocumentMetadataPanel
│   ├── upload/               # DocumentUpload, UploadFileRow, ProcessingStatus
│   ├── chat/                 # ChatWindow, ChatMessage, ChatInput, ScopeSelector, SuggestedQuestions
│   ├── citations/            # CitationBadge, SourcePreview, CitationList, SourceHighlight
│   ├── viewer/                # DocumentViewer, TableOfContents, InDocumentSearch, PageNavigator
│   ├── search/                # SearchBar, SearchResultCard, SearchModeSelector
│   ├── comparison/            # ComparisonSelector, DiffViewer, ChangeCard, ComparisonSummary
│   ├── conflicts/              # ConflictCard, ConflictResolutionMenu
│   ├── summary/                 # SummarySection, TopicTagList
│   ├── analytics/                # KpiCard, LatencyBreakdownChart, CostBreakdownTable
│   ├── settings/                  # SettingsNav, UserTable, RolePermissionMatrix, AuditLogTable
│   └── common/                     # Button, Input, Badge, Modal, Tooltip, Dropdown, Tabs, EmptyState, Skeleton
├── hooks/                    # useDocumentProcessingStream, useCitationNavigation, usePanelResize…
├── lib/
│   ├── api/                  # typed fetch clients per API category
│   ├── realtime/              # SSE/WebSocket client, event dispatcher
│   └── query/                  # React Query client config, query keys
├── state/                     # Zustand stores (ui, viewer, chat-scope, panels)
└── types/                      # shared TS types/interfaces (Document, Citation, Change, Conflict…)
```

### 8.2 Key Component Specifications

> Format: **Responsibility · Props · State · Events · Dependencies · Reusability**

**`Sidebar`**
- *Responsibility:* primary app navigation, collapse/expand.
- *Props:* `activeRoute: string`, `collapsed: boolean`, `onToggleCollapse: () => void`.
- *State:* none owned (collapse state lifted to global UI store).
- *Events:* `onNavigate(route)`.
- *Dependencies:* `useUiStore`, router.
- *Reusability:* single instance, app-shell-only.

**`Header`**
- *Responsibility:* global search entry, processing indicator, notifications, user menu.
- *Props:* `user: User`, `org: Organization`.
- *State:* command palette open/closed (local).
- *Events:* `onOpenCommandPalette`, `onSignOut`.
- *Dependencies:* `useProcessingJobs` (polls/subscribes to active jobs), `useAuth`.
- *Reusability:* single instance.

**`DocumentTable`**
- *Responsibility:* paginated/sortable/filterable document listing with bulk actions.
- *Props:* `filters: DocumentFilters`, `sort: SortState`, `page: number`, `selection: string[]`, `onSelectionChange`, `onSortChange`, `onRowClick(doc)`.
- *State:* none (controlled component; parent page owns filter/sort/page state, often synced to URL query params).
- *Events:* `onSort`, `onSelect`, `onBulkAction(action, ids)`.
- *Dependencies:* `useDocuments(filters, sort, page)` (React Query), `StatusBadge`, `Pagination`, `BulkActionBar`.
- *Reusability:* high — reused (in a constrained/embedded mode) inside `DocumentSelector` and Collection detail views.

**`DocumentCard`**
- *Responsibility:* compact document representation for grid view and mobile row-collapse.
- *Props:* `document: DocumentSummary`, `selected?: boolean`, `onSelect?`, `onOpen`.
- *State:* none.
- *Events:* `onClick`, `onMenuAction(action)`.
- *Dependencies:* `StatusBadge`.
- *Reusability:* high — Dashboard "Recently Uploaded," Documents grid view, Search results (document-level grouping).

**`DocumentUpload`**
- *Responsibility:* dropzone + file intake + metadata form orchestration.
- *Props:* `defaultCollectionId?`, `onUploadComplete(documentIds[])`.
- *State:* files-in-flight list, per-file upload/processing status (via `useDocumentProcessingStream` per file).
- *Events:* `onFilesAdded`, `onFileRemove`, `onSubmit`.
- *Dependencies:* `useUploadDocument` (mutation), `useDocumentProcessingStream`, `ProcessingStatus`, `CollectionSelect`, `TagInput`.
- *Reusability:* medium — used as modal (Dashboard/Documents) and full-page route.

**`ProcessingStatus`**
- *Responsibility:* renders the step-tracker (Uploaded → Extracted → Structure → OCR → Embeddings → Indexing) for one document, driven by live events.
- *Props:* `documentId: string`, `initialStatus?: DocumentStatus`.
- *State:* current step, per-step sub-progress, error state — derived from subscribed stream events.
- *Events:* `onRetry()`.
- *Dependencies:* `useDocumentProcessingStream(documentId)`.
- *Reusability:* high — upload modal, Document Workspace (while processing), Dashboard processing-activity list, header processing dropdown.

**`DocumentViewer`**
- *Responsibility:* renders document pages (PDF.js), manages zoom/page-nav, hosts the highlight overlay layer.
- *Props:* `documentId`, `version`, `initialPage?`, `highlight?: SourceHighlightSpan`, `onPageChange(page)`.
- *State:* current page, zoom level, loaded-page cache, text layer selection.
- *Events:* `onPageChange`, `onSearchInDocument(query)`.
- *Dependencies:* PDF.js, `SourceHighlight`, `InDocumentSearch`, `PageNavigator`.
- *Reusability:* single core implementation, reused across Document Workspace, Source Evidence panel (constrained/embedded mode showing a single page), and any "open in document" deep link target.

**`TableOfContents`**
- *Responsibility:* renders the section/subsection tree, scrollspy-syncs with viewer, exposes conflict markers.
- *Props:* `tree: TocNode[]`, `activeSectionId?`, `onNavigate(sectionId)`.
- *State:* expanded/collapsed node set.
- *Events:* `onNavigate`.
- *Dependencies:* none beyond data.
- *Reusability:* single primary use (Document Workspace left panel); shape reused by `SectionNavigator` in Comparison.

**`ChatWindow`**
- *Responsibility:* orchestrates a conversation's message list + input + streaming lifecycle.
- *Props:* `conversationId?`, `scope: ChatScope`, `onScopeChange`.
- *State:* draft input text, streaming buffer (owned via a hook, not local state, so it can survive route transitions within the same session).
- *Events:* `onSend(message)`, `onRegenerate(messageId)`, `onStop()`.
- *Dependencies:* `useConversation`, `useSendMessage` (streaming mutation), `ChatMessage`, `ChatInput`, `ScopeSelector`.
- *Reusability:* high — Ask AI full page and Three-Panel Research Workspace center panel are the same component in different container layouts.

**`ChatMessage`**
- *Responsibility:* renders one turn (user or assistant), including inline citations and per-message actions.
- *Props:* `message: Message`, `isStreaming?: boolean`, `onCitationClick(citation)`, `onFeedback(value)`, `onRegenerate`, `onCopy`.
- *State:* none (presentational).
- *Events:* `onCitationClick`, `onFeedback`, `onRegenerate`, `onCopy`.
- *Dependencies:* `CitationBadge`, `SourcePreview`.
- *Reusability:* high.

**`ChatInput`**
- *Responsibility:* message composition, `@document` mention, send/stop control.
- *Props:* `value`, `onChange`, `onSend`, `disabled?`, `isStreaming?`, `onStop?`.
- *State:* mention-picker open/closed.
- *Events:* `onSend`, `onMentionSelect(document)`.
- *Dependencies:* `DocumentSelector` (mention popover variant).
- *Reusability:* single primary use, shared between Ask AI and Research Workspace.

**`CitationBadge`**
- *Responsibility:* the interactive numbered citation marker.
- *Props:* `citation: Citation`, `index: number`, `active?: boolean`, `onClick`, `onHover`.
- *State:* hover/focus (local, for popover trigger).
- *Events:* `onClick`, `onFocus`.
- *Dependencies:* `SourcePreview` (renders on hover/focus).
- *Reusability:* very high — chat messages, summaries, change cards, conflict cards, search-result-to-source links.

**`SourcePreview`**
- *Responsibility:* popover/sheet showing exact source text in context for a citation.
- *Props:* `citation: Citation`, `anchorRef`, `onOpenSource()`.
- *State:* fetch state for source text if not pre-loaded.
- *Events:* `onOpenSource`.
- *Dependencies:* `useSourceContent(citation)`.
- *Reusability:* high — same as `CitationBadge`'s callers.

**`DocumentSelector`**
- *Responsibility:* searchable, filterable checkbox picker for choosing document scope.
- *Props:* `selected: string[]`, `onChange`, `mode: 'single' | 'multi'`, `constraints?` (e.g., exclude non-READY docs).
- *State:* internal search query, filter state.
- *Events:* `onChange`, `onApply`.
- *Dependencies:* `useDocuments` (lightweight list query).
- *Reusability:* very high — Ask AI scope, Research Workspace, Comparison Document A/B pickers, `@mention` in `ChatInput`.

**`SearchResult`** *(`SearchResultCard`)*
- *Responsibility:* one search hit — document, section/page, snippet, relevance, actions.
- *Props:* `result: SearchResult`, `onOpen`, `onAskAi`.
- *State:* none.
- *Events:* `onOpen`, `onAskAi`.
- *Dependencies:* `RelevanceIndicator`.
- *Reusability:* medium — Search page primary use; shape echoed (not necessarily shared component) in "suggested sources" contexts.

**`ComparisonSelector`**
- *Responsibility:* the two-document/version picker that initiates a comparison.
- *Props:* `documentA?`, `documentB?`, `onChange`, `onCompare`.
- *State:* local selection before submit.
- *Events:* `onCompare(a, b)`.
- *Dependencies:* `DocumentSelector` (single mode ×2).
- *Reusability:* single primary use; logic reused by "Compare" quick action from Document Workspace (pre-filled variant).

**`ChangeCard`**
- *Responsibility:* single change unit (see [§6.11](#611-change-detection)).
- *Props:* `change: Change`, `collapsed?`, `onToggleCollapse`, `onViewSources`, `onOpenInDocument`.
- *State:* collapsed/expanded (controllable or uncontrolled).
- *Events:* `onViewSources`, `onOpenInDocument`.
- *Dependencies:* `CitationBadge`.
- *Reusability:* high — Comparison changes-list, and reused as the visual basis for `ConflictCard` (same old/new pattern, different framing/icon).

**`DiffViewer`**
- *Responsibility:* renders the full side-by-side/unified document diff.
- *Props:* `comparisonId`, `viewMode: 'split' | 'unified' | 'list'`, `severityFilter`, `syncScroll: boolean`.
- *State:* scroll position sync, active section.
- *Events:* `onSelectChange(change)`.
- *Dependencies:* `useComparison(comparisonId)`, `SectionNavigator`, `ChangeCard`.
- *Reusability:* single primary use (Comparison results view).

### 8.3 Common/Shared Primitives

`Button`, `IconButton`, `TextInput`, `Select`, `Checkbox`, `RadioGroup`, `Badge`, `StatusBadge`, `Card`, `Table`, `Pagination`, `Modal`, `Sheet` (mobile bottom-sheet), `Tooltip`, `Popover`, `Dropdown`/`Menu`, `Tabs`, `Skeleton`, `EmptyState`, `InlineAlert`, `Toast`, `ResizablePanelGroup` — all live in `components/common/` and are the only components allowed to define raw Tailwind design-token usage; feature components consume them rather than re-implementing visual primitives.

---

## 9. State Management

### 9.1 Server State — React Query

All data owned by the backend is fetched/cached/mutated exclusively through React Query. Rationale: automatic caching, background refetch, request de-duplication, and built-in loading/error states map directly onto the UX-state requirements in [§18](#18-error--loading--empty-states).

| Domain | Query keys (example) | Notes |
|---|---|---|
| Documents | `['documents', filters, sort, page]`, `['document', id]` | List invalidated on upload/bulk-action/status-change events |
| Processing status | not cached via polling — driven by realtime stream ([§11](#11-real-time-processing-ux)), written into the query cache via `queryClient.setQueryData` on each event |
| Conversations/messages | `['conversations']`, `['conversation', id, 'messages']` | Messages list appended optimistically on send, reconciled on stream completion |
| Search results | `['search', query, mode, filters, page]` | Not cached long (short `staleTime`) since relevance is often exploratory/one-off |
| Comparisons | `['comparison', id]` | Long `staleTime` — comparison results are immutable once computed |
| Summaries | `['summary', documentId, version]` | Invalidated on regenerate |
| Conflicts | `['conflicts', status]` | Invalidated on resolve action |
| Analytics | `['analytics', metric, range]` | Background refetch on interval (e.g., 60s) while Analytics tab is active |
| Users/roles/settings | `['users']`, `['roles']`, `['settings', section]` | Standard CRUD invalidation |

**Mutation pattern:** every mutation (upload, bulk action, send message, resolve conflict, save settings) uses `useMutation` with explicit `onSuccess` cache invalidation/update — optimistic updates used selectively (see below), never blindly.

**Optimistic updates — where justified:**
- **Chat message send:** the user's message renders immediately (optimistic), assistant placeholder shows the staged loading label; rolled back only on hard send failure (not on stream errors after the message was accepted).
- **Bulk document actions (tag/move):** table rows update immediately with a pending visual state, reconciled/rolled back on error with the partial-failure toast pattern described in [§6.3](#63-documents-page).
- **NOT optimistic:** deletes, access-level changes, conflict resolution, comparison runs — these have real consequences or real latency where premature success feedback would mislead the user.

### 9.2 Client/UI State — Zustand (+ narrow local `useState`/Context)

Client-only state is anything that doesn't need to survive a hard refresh authoritatively from the server and isn't itself a server resource:

| State | Store | Why not React Query |
|---|---|---|
| Selected documents (chat/comparison scope) | Zustand `useChatScopeStore` | Ephemeral selection, not a server resource; needs to be read by multiple components (ChatInput, ScopeSelector, Header indicator) without prop drilling |
| Active panel layout (Research Workspace widths/collapse) | Zustand `usePanelStore`, persisted to `localStorage` | Pure UI preference |
| Sidebar collapsed state | Zustand `useUiStore`, persisted | Pure UI preference |
| Viewer state (current page, zoom, active highlight) | Zustand `useViewerStore` | High-frequency local interaction state; would cause excessive query-cache churn if modeled as server state |
| Citation panel focus (which citation is "active") | Zustand `useViewerStore` (co-located with viewer state) | Drives cross-panel synchronization in the Research Workspace |
| Command palette open/closed, modal stacks | local `useState` / React Context at the app-shell level | Truly local, single-consumer |
| Form drafts (e.g., upload metadata before submit) | local `useState` within the form component | Scoped to the component's lifetime |

**Why the split matters:** conflating server state and client state in one store (e.g., putting document lists in Zustand) forfeits React Query's cache invalidation, refetch-on-focus, and de-duplication for no benefit — and conversely, putting ephemeral UI state (panel widths) into React Query would require fake query functions and pollute the server-cache mental model. The rule of thumb used throughout this spec: **if the backend has an endpoint for it, it's React Query; if it only exists to make the UI usable, it's Zustand/local state.**

---

## 10. API Integration

### 10.1 API Categories

```text
/auth            — login, register, password reset, session
/documents       — CRUD, upload, versions, TOC, content, search-in-doc, retry
/chat            — conversations, messages (streaming), feedback
/search          — knowledge-base search (hybrid/semantic/keyword)
/comparison       — run + retrieve comparisons, changes
/conflicts         — list/detail/resolve
/summaries          — get/regenerate document summaries
/analytics           — usage, latency, quality, cost metrics
/users                — user management
/settings              — org/AI/document/security/integration config
/audit-log              — audit trail
```

### 10.2 Example Contracts

**`POST /documents` (upload)**
```json
// multipart/form-data: file, collectionId, accessLevel, tags[]
// response (202 Accepted)
{
  "documentId": "doc_8f2a",
  "status": "UPLOADED",
  "fileName": "Marketing_Policy_2026.pdf"
}
```

**`GET /documents/:id`**
```json
{
  "id": "doc_8f2a",
  "name": "Marketing Policy 2026",
  "type": "Policy",
  "department": "Marketing",
  "status": "READY",
  "currentVersion": "2.1",
  "owner": { "id": "u_1", "name": "J. Doe" },
  "updatedAt": "2026-08-24T10:15:00Z",
  "collectionId": "col_hr",
  "accessLevel": "organization",
  "tags": ["policy", "2026"]
}
```

**`POST /chat/conversations/:id/messages` (streamed via SSE)**
```json
// request
{ "content": "What is the approval process?", "scope": { "type": "documents", "documentIds": ["doc_8f2a", "doc_91cd"] } }

// SSE event stream
event: token
data: {"delta": "The approval process contains"}

event: token
data: {"delta": " four stages."}

event: citation
data: {"index": 1, "documentId": "doc_8f2a", "version": "2.1", "page": 12, "section": "4.2", "text": "...four sequential stages..."}

event: done
data: {"messageId": "msg_44", "groundedness": "grounded"}
```

**`POST /comparison`**
```json
// request
{ "documentAId": "doc_8f2a", "versionA": "1.0", "documentBId": "doc_8f2a", "versionB": "2.1" }
// response
{ "comparisonId": "cmp_12", "status": "PROCESSING" }
```

**`GET /comparison/:id/changes`**
```json
{
  "summary": { "total": 12, "major": 4, "moderate": 5, "minor": 3 },
  "changes": [
    {
      "id": "chg_1",
      "type": "modified",
      "severity": "moderate",
      "section": "3.1 Approval Process",
      "old": { "text": "...within 5 business days.", "citation": { "documentId": "doc_8f2a", "version": "1.0", "page": 9, "section": "3.1" } },
      "new": { "text": "...within 7 business days.", "citation": { "documentId": "doc_8f2a", "version": "2.1", "page": 12, "section": "3.1" } }
    }
  ]
}
```

### 10.3 Cross-Cutting Integration Behaviors

- **Loading states:** every query-backed component distinguishes *initial load* (skeleton) from *background refetch* (subtle top-bar progress, content stays visible) — React Query's `isLoading` vs `isFetching` map directly to this.
- **Error handling:** a shared `ApiError` shape (`{ code, message, field? }`) is parsed centrally by the API client; components render either a field-level error (validation) or a component-scoped `InlineAlert` (everything else) — the app never shows a raw stack trace or unparsed error string to the user.
- **Retry behavior:** transient errors (network, 5xx) auto-retry twice with exponential backoff at the query-client level (React Query default, tuned to 2 retries); 4xx errors (validation, permission) never auto-retry — surfaced immediately.
- **Pagination:** all list endpoints use cursor or page+pageSize (consistent per-resource, documented per endpoint); `DocumentTable`/`SearchResult`/`AuditLogTable` use the shared `Pagination` component and a shared `usePaginatedQuery` hook.
- **Streaming responses:** chat responses stream via SSE (`text/event-stream`) with typed events (`token`, `citation`, `error`, `done`) as shown above; the client buffers `token` deltas into the message state and attaches `citation` events to the growing citation list, finalizing on `done`.
- **WebSocket/SSE for processing:** see [§11](#11-real-time-processing-ux) in full.
- **Optimistic updates:** see [§9.1](#91-server-state--react-query).

---

## 11. Real-Time Processing UX

### 11.1 Transport

**Server-Sent Events (SSE)** is the default transport for document-processing progress and chat streaming — simpler than WebSocket for the mostly-unidirectional (server→client) event flow this product needs, with automatic reconnection built into the `EventSource` API. **WebSocket** is reserved for genuinely bidirectional needs if introduced later (e.g., live multi-user collaboration cursors) — not required for v1.

### 11.2 Document Processing Stream

```text
Client                                   Server
  │  GET /documents/:id/stream (SSE)        │
  │ ───────────────────────────────────────▶│
  │                                           │
  │  event: status  {step:"extracting"}      │
  │ ◀───────────────────────────────────────│
  │  event: status  {step:"ocr", progress:40}│
  │ ◀───────────────────────────────────────│
  │  event: status  {step:"embedding", ...}  │
  │ ◀───────────────────────────────────────│
  │  event: status  {step:"ready"}           │
  │ ◀───────────────────────────────────────│
  │  (connection closes)                     │
```

- One SSE connection per actively-tracked document (opened when: the upload modal is showing it, the Document Workspace has it open and it's non-`READY`, or the header processing dropdown is expanded).
- A single **multiplexed** connection (`/documents/stream?ids=doc_1,doc_2,doc_3`) is used when tracking several documents at once (e.g., the header indicator tracking all org-wide active jobs), to avoid opening N connections.
- The `useDocumentProcessingStream` hook owns connection lifecycle (open on mount/status-non-terminal, close on `ready`/`failed`/unmount) and writes each event directly into the React Query cache for `['document', id]`, so every component reading that document's status (table row, workspace header, upload row) updates in lockstep without redundant polling.

### 11.3 Reconnection & Fallback

- `EventSource` auto-reconnects on drop; the hook additionally treats a reconnect as a signal to **re-fetch current status via REST** (`GET /documents/:id`) immediately, in case events were missed during the gap — the stream is a live-update optimization, not the source of truth.
- If SSE is unavailable (e.g., corporate proxy blocking it), the hook falls back to polling `GET /documents/:id` every 3s while status is non-terminal; this fallback is transparent to consuming components (same cache write path).

### 11.4 Chat Streaming

- Same SSE mechanism, scoped per in-flight message (`POST /chat/conversations/:id/messages` returns a stream directly rather than a connection to subscribe to separately).
- A **stop generating** control sends `POST /chat/messages/:id/stop` and the client immediately freezes the rendered partial text as final (with a subtle "Stopped" marker) rather than waiting for the server to confirm.

### 11.5 UX Consequences

- Every status-driving UI element (`StatusBadge`, `ProcessingStatus`) is a pure function of the query cache — there is exactly one place that mutates processing state client-side, preventing divergent status displays across the Dashboard/Documents table/Workspace/header simultaneously showing a document.
- Network loss during processing is surfaced subtly (small "reconnecting…" indicator on the affected `ProcessingStatus` component) rather than as an app-wide error — processing continues server-side regardless of the client's connection.

---

## 12. Citation UX (Deep Dive)

This section consolidates the citation interaction model referenced throughout [§6.7](#67-ai-answer--citation-experience), [§6.8](#68-three-panel-research-workspace), [§6.9](#69-search), [§6.11](#611-change-detection), [§6.12](#612-document-summary), and [§6.13](#613-conflict-detection) into one canonical spec, since citations are the single most reused interaction pattern in the product.

**Citation object shape (client-side type):**
```ts
interface Citation {
  id: string
  documentId: string
  documentName: string
  version: string
  page: number
  section: string           // e.g. "4.2 Regulatory Review"
  sourceText: string        // exact span
  contextBefore?: string    // surrounding text for the popover
  contextAfter?: string
  effectiveDate?: string
  boundingBox?: { page: number; x: number; y: number; width: number; height: number } // for viewer overlay
}
```

**Canonical interaction contract** (every surface that renders a citation must honor this):
1. **Rest state** — numbered badge, visually part of the sentence flow but distinguishable (never plain text bracket numbers indistinguishable from body copy).
2. **Hover/focus** — `SourcePreview` popover within ~150ms, showing `sourceText` bolded within `contextBefore`/`contextAfter`, document name, page, section.
3. **Click/activate** — resolves to one of two behaviors depending on context:
   - **Panel context** (Research Workspace): updates the Source Evidence panel in place.
   - **Standalone context** (Ask AI single-column, Search, Summary, Change/Conflict cards): navigates into the Document Workspace viewer at `page`, with `SourceHighlight` applied.
4. **Highlight persistence** — the highlight in the viewer remains until the user navigates to a different page/citation or explicitly dismisses it (a small "×" on the highlight overlay); it is not a transient flash.
5. **Multi-citation claims** — each number is independently interactive; there is no "merged" citation state.
6. **Missing/broken citation** — rendered in a visually distinct muted/dashed state with a tooltip explaining why (source deleted, access revoked) rather than a dead or misleading link.

**Why this is centralized:** because citations appear in 7+ distinct screens, a single `CitationBadge` + `SourcePreview` + `useCitationNavigation` hook implementation (not 7 bespoke ones) is what makes the explainability principle ([§3.1](#31-the-explainability-principle-governing-principle-of-the-whole-product)) actually consistent for users, and is the single highest-leverage shared component in this system.

---

## 13. Document Viewer UX (Deep Dive)

**Rendering:** PDF.js (or equivalent) renders pages canvas-based with an accompanying selectable text layer whenever the source PDF has one (native PDFs, and OCR'd scans where OCR text layer injection is performed server-side). Pure-image scans without usable OCR fall back to image-only rendering with a visible "OCR unavailable — text search/selection disabled for this document" notice.

**Core controls (toolbar):**
- Page navigation: `‹ Page 12 of 40 ›` with direct numeric entry.
- Zoom: `−  100%  +`, fit-to-width / fit-to-page toggle.
- In-document search (`InDocumentSearch`): match count + next/prev, distinct from global Search.
- Expand/fullscreen toggle (`⤢`) — especially relevant when embedded in the constrained Source Evidence panel.

**Highlight overlay (`SourceHighlight`):**
- Rendered as an absolutely-positioned layer above the canvas, using the citation's `boundingBox` (page-relative coordinates returned by the backend's citation-generation step).
- Visual treatment: soft yellow/amber wash background + a subtle left accent bar, consistent with the "modified/evidence" color used elsewhere, chosen to be distinguishable from selection-blue (browser native text selection) and from the diff-view amber (modified) without a jarring clash — final token values in [§17](#17-design-system).
- When a new citation is activated while one is already highlighted, the previous highlight clears (single-focus model — multiple simultaneous highlights would undermine "the exact text" clarity).

**Navigation sources into the viewer:** citation click, search result "Open," TOC node click, Change/Conflict card "Open in document," version switch (re-renders at the same logical section if possible, falling back to page 1 with a notice if the section doesn't exist in the target version).

**Performance considerations (spec-level, not implementation):** pages render lazily (virtualized — only near-viewport pages mounted) given documents can run hundreds of pages; the TOC/page-jump must not require sequential rendering of all intervening pages.

**States:** covered in [§6.5](#65-document-details--document-workspace) (loading/processing/error/permission-denied); additionally, a **zoom/page state persists** per document within a session (so returning from a citation click elsewhere and back doesn't reset the user's reading position unexpectedly) — except when a fresh citation navigation intentionally overrides it.

---

## 14. Comparison UX (Deep Dive)

This section consolidates the diff/severity/navigation model shared by [§6.10](#610-document-comparison) and [§6.11](#611-change-detection).

**Diff granularity:** section-level grouping (what the TOC/SectionNavigator shows change-density for) containing paragraph/sentence-level change blocks, with **word-level highlighting inside Modified blocks** — this three-tier granularity is what lets a user triage at the section level, read at the paragraph level, and verify the exact edit at the word level, without three different tools.

**Severity computation (surfaced, not just described):** severity is a backend-computed classification exposed as a first-class filterable/sortable attribute on every change — the frontend never infers severity from text length or type alone; it always trusts and displays the server-provided tier, with the three-tier taxonomy (Major/Moderate/Minor) kept intentionally small so it stays scannable across a 12+ change comparison.

**View mode selection logic:**
| Viewport | Default mode | User-switchable? |
|---|---|---|
| ≥1024px (desktop) | Split (side-by-side) | Yes — Split / Unified / List |
| 768–1023px (tablet) | Unified | Yes — Unified / List (Split disabled) |
| <768px (mobile) | List | No — List only |

**Cross-document vs. cross-version comparison:** the UI does not visually distinguish these as different modes — both flow through the same `ComparisonSelector` → `DiffViewer` pipeline — but the header always names both sides explicitly by document+version (e.g., "Marketing Policy 2025 ↔ Marketing Policy 2026" vs. "Vendor Draft Contract ↔ Standard Template v4") so the user's mental model of *what* is being compared is never ambiguous.

**Relationship to Conflict Detection:** Comparison is user-initiated (I chose these two documents); Conflict Detection is system-initiated (the system noticed these two documents disagree) — but both terminate in the same `DiffViewer`/`ChangeCard` visual language so a user's learned mental model transfers between the two without relearning an interaction pattern.

---

## 15. Responsive Design

### 15.1 Breakpoints

| Token | Width | Primary devices |
|---|---|---|
| `sm` | ≥640px | Large phones (landscape) |
| `md` | ≥768px | Tablets (portrait) |
| `lg` | ≥1024px | Tablets (landscape) / small laptops |
| `xl` | ≥1280px | Laptops/desktops |
| `2xl` | ≥1536px | Large desktops |

### 15.2 Desktop-First vs. Fully Responsive Surfaces

| Screen/experience | Classification | Rationale |
|---|---|---|
| Three-Panel Research Workspace | **Desktop-first (≥1280px for 3-panel; 2-panel down to `lg`; unavailable below `md`, redirects to Ask AI)** | Synchronized triple-context view is not meaningfully compressible below a certain width without losing the point of the feature |
| Document/PDF Viewer | **Desktop-first for editing/annotation-adjacent workflows; read-capable responsive down to mobile** | Reading and citation-following must work on mobile (real use case: reviewing a citation from a mobile-received link); heavy interaction (TOC + viewer side-by-side) is desktop-optimized |
| Document Comparison (Split view) | **Desktop-first (≥1024px)** | Side-by-side diff is unreadable below this; auto-degrades to Unified/List (see [§14](#14-comparison-ux-deep-dive)) rather than being disabled |
| Ask AI / Chat | **Fully responsive** | Conversational Q&A is a natural mobile use case |
| Search | **Fully responsive** | Quick lookup is a natural mobile use case |
| Document List/Table | **Fully responsive** (table → card list) | Browsing/triage happens everywhere |
| Dashboard | **Fully responsive** (grid reflow) | Overview-checking is a natural mobile use case |
| Upload | **Responsive, drag-and-drop de-emphasized on touch** | Supported everywhere; optimized for desktop batch upload |
| Analytics | **Responsive with simplified mobile charts** | Monitoring happens on the go; deep analysis happens at a desk |
| Settings | **Fully responsive** | Administrative but not layout-intensive |

### 15.3 General Collapse Rules

- **Sidebar:** auto-collapses to icon rail at `<lg`; becomes a slide-over drawer (hidden by default, hamburger-triggered) at `<md`.
- **Multi-panel layouts:** collapse outer panels to drawers/sheets before collapsing the primary content panel — the primary content (viewer, chat transcript, table) is always the last thing to lose space.
- **Tables → cards:** any data table (`DocumentTable`, `UserTable`, `AuditLogTable`) converts to a stacked card list at `<md`, preserving the same primary/secondary field hierarchy (primary identifier + status always visible; secondary metadata behind a `⋯`/expand affordance).
- **Modals → sheets:** modals become bottom sheets on touch devices for better thumb-reachability.
- **Filter bars:** collapse into a single "Filters" trigger opening a sheet, rather than wrapping inline and consuming vertical space.

---

## 16. Accessibility

### 16.1 Baseline Commitments

- Target **WCAG 2.1 AA** across the product.
- All interactive elements reachable and operable via keyboard alone; no mouse-only affordances (hover-only popovers always have a focus-triggered equivalent, per [§12](#12-citation-ux-deep-dive)).
- Color is never the sole carrier of meaning — status badges, diff states, severity, and relevance always pair color with an icon and/or text label.
- Minimum contrast ratio 4.5:1 for body text, 3:1 for large text/icons, enforced by the design tokens in [§17](#17-design-system).

### 16.2 Keyboard Navigation & Shortcuts

| Shortcut | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Open command palette |
| `⌘/` / `Ctrl+/` | Show keyboard shortcut reference |
| `G` then `D` | Go to Dashboard |
| `G` then `A` | Go to Ask AI |
| `G` then `S` | Go to Search |
| `Enter` | Send chat message (input focused) |
| `Shift+Enter` | Newline in chat input |
| `Esc` | Close modal/popover/sheet; clear an active highlight |
| `Tab`/`Shift+Tab` | Cycle citations within a message |
| `↑`/`↓` | Navigate table rows / list results |
| `Ctrl+F` (in-viewer, when viewer focused) | Open in-document search rather than browser find, with a visible hint if intercepted |

### 16.3 Focus Management

- Opening a modal/sheet moves focus to its first focusable element (usually its heading or first input) and traps focus within it (`Esc` and explicit close both return focus to the trigger element).
- Route changes (e.g., clicking a citation that navigates to the Document Workspace) move focus to the page's main heading, not silently leave focus on a now-unmounted element.
- Streaming chat text does **not** steal focus or scroll-jump away from a user who has scrolled up to re-read earlier context; a "Jump to latest" affordance appears instead when new content arrives off-screen.

### 16.4 ARIA & Semantics Highlights

- `DocumentTable`/`UserTable`/`AuditLogTable`: real `<table>` with `<th scope="col">`, sortable headers as `<button aria-sort="ascending|descending|none">`.
- `TableOfContents`: `<nav aria-label="Table of contents">` with a proper nested list, `aria-current="true"` on the active node.
- `CitationBadge`: `<button aria-describedby="popover-id" aria-label="Citation 1: Marketing Policy 2026, page 12">`.
- `StatusBadge`: text content always present (e.g., "Ready", "Processing · Embedding"), icon is `aria-hidden="true"`.
- Streaming regions: a visually-hidden `aria-live="polite"` summary announces "Answer received" on completion rather than announcing every token; the visible text itself is not inside a live region (to avoid a firehose of announcements).
- `Modal`/`Sheet`: `role="dialog" aria-modal="true"`, labeled via `aria-labelledby`.
- `RolePermissionMatrix`, `ResizablePanelGroup`: custom `role="grid"`/appropriate ARIA per WAI-ARIA Authoring Practices patterns for grid and window-splitter widgets respectively.

### 16.5 Accessible Dialogs & Tables

- Confirmation dialogs (bulk delete, access-level change, SSO enforcement) always state the action and consequence in plain text within the dialog body (not just the title), with the destructive action never being the default-focused button.
- Wide data tables (Analytics cost breakdown, Audit Log with many columns) provide a horizontally-scrollable container with a visible scroll affordance, and critical columns (name/date/actor) are sticky-positioned so context isn't lost while scrolling.

---

## 17. Design System

### 17.1 Visual Direction

**Professional, clean, modern, trustworthy, information-dense without clutter.** No gradients-as-decoration, no glowing "AI" auras, no chat-bubble skeuomorphism. Reference points: Linear, Retool, Notion (team/enterprise surfaces), GitHub's data-dense views — not consumer chat products.

### 17.2 Typography

| Token | Size / Line-height | Use |
|---|---|---|
| `text-xs` | 12px / 16px | Metadata, timestamps, badges |
| `text-sm` | 14px / 20px | Body default in dense UI (tables, lists) |
| `text-base` | 16px / 24px | Primary reading content (chat answers, document summary prose) |
| `text-lg` | 18px / 28px | Section headings within a panel |
| `text-xl` | 20px / 28px | Page titles |
| `text-2xl` | 24px / 32px | Dashboard KPI numbers |

Font: a single system/enterprise sans-serif (e.g., Inter or the system UI stack) for UI chrome; a monospace fallback reserved only for structured technical values if ever needed (not used in v1 screens). Weight scale: 400 (body), 500 (labels/emphasis), 600 (headings/KPIs).

### 17.3 Spacing & Layout

- Base unit: **4px**. Scale: `1` (4px) `2` (8px) `3` (12px) `4` (16px) `6` (24px) `8` (32px) `12` (48px) `16` (64px).
- Panel padding: 16–24px. Table cell padding: 8–12px vertical, 12–16px horizontal. Card padding: 16px.

### 17.4 Borders, Radius, Shadow

- Border: 1px, low-contrast neutral (`border-subtle`), used generously for structure over shadow.
- Radius: `sm` 4px (badges, inputs), `md` 6px (buttons, cards), `lg` 8px (modals, panels).
- Shadow: reserved for elevation above the base surface only — popovers (`shadow-md`), modals (`shadow-lg`). Cards and panels use borders, not shadows, to stay flat and dense-friendly.

### 17.5 Color System

| Role | Token | Use |
|---|---|---|
| Primary | `accent` | Primary buttons, active nav, active citation, links |
| Neutral scale | `gray-50…900` | Text, borders, backgrounds |
| Success | `green` | `READY` status, added-diff, positive empty states |
| Info/Processing | `blue` | Non-terminal processing statuses, informational badges |
| Warning | `amber` | `Modified` diff, Moderate severity, conflict warnings |
| Danger | `red` | `FAILED` status, removed-diff, Major severity, destructive actions |
| Evidence highlight | `amber-100` wash | Source-text highlight overlay in the viewer |

Status/severity color mapping is **fixed and reused everywhere** (Documents table, Comparison, Conflicts, Analytics) — red never means "removed" in one screen and "error" only in another; the semantic mapping is: red = negative/failed/major/removed, amber = caution/moderate/modified, green = success/ready/added, blue = in-progress/info.

### 17.6 Core Components (visual spec summary)

- **Buttons:** Primary (filled accent), Secondary (outlined/neutral), Ghost (text-only, for row-level actions), Destructive (filled red, requires confirmation for high-impact actions). Sizes: sm (32px), md (36px, default), lg (44px).
- **Inputs:** consistent 36px height, 1px border, visible label above (not placeholder-as-label), focus ring in `accent` at 2px offset.
- **Cards:** 1px border, `md` radius, 16px padding, optional header row with title + actions.
- **Tables:** zebra-free (border-based row separation), sticky header on scroll, hover row highlight, sortable column affordance.
- **Badges:** `StatusBadge` (status-colored, icon+text), `SeverityBadge` (Major/Moderate/Minor), neutral tag badges (topics, tags) — all `sm` radius, `text-xs`, 4px/8px padding.
- **Alerts (`InlineAlert`):** left-accent-bar style (not full-color background) for info/warning/error/success, consistent with the diff-block visual language elsewhere in the product.
- **Modals:** centered, `lg` radius, `shadow-lg`, max-width tiers (sm 400px / md 560px / lg 800px), header + scrollable body + sticky footer action row.
- **Tooltips:** dark neutral background, `text-xs`, 200ms delay on hover, immediate on focus.
- **Dropdowns/Menus:** `md` radius, `shadow-md`, 4px item padding, destructive items visually separated (divider + red text).
- **Tabs:** underline-style for section-level navigation (e.g., Comparison's Summary/Split/List), pill-style for compact mode toggles (e.g., Search mode selector).

---

## 18. Error / Loading / Empty States

Every screen's individual state definitions are detailed in [§6](#6-screen-by-screen-design); this matrix summarizes the pattern applied consistently across the product so implementers can verify coverage.

| Screen | Loading | Empty | Success | Error | Processing | Disabled | Permission denied | No results | Partial |
|---|---|---|---|---|---|---|---|---|---|
| Dashboard | Per-widget skeleton | New-org empty state | KPI + activity lists | Per-widget retry | — | — | Analytics-linked KPIs hidden if unauthorized | — | Widget hides if count=0 (e.g., Failed Jobs) |
| Documents | Skeleton rows | "No documents" + Upload CTA | Table/grid | Banner + retry | Row-level status badge | Bulk actions disabled if 0 selected | Actions hidden/disabled per role | "No matches" + clear filters | Bulk action partial-success toast |
| Upload | Byte progress bar | Empty dropzone prompt | Step tracker complete | Field/file-level inline error | Step tracker mid-flight | Submit disabled until valid | Access options limited by role | — | Some files succeed, some fail — per-file state |
| Doc Workspace | Header/TOC/viewer skeletons | "No structure detected" (TOC) | Full workspace | Full-panel retry | Step-tracker in place of viewer | AI actions disabled while processing | Access-denied dedicated state | — | Version banner if non-current |
| Ask AI | Skeleton conversation list | Empty-state w/ example questions | Streamed answer | Retry-preserving-question | Staged loading label | Input disabled with 0 scope docs | — | "No grounded answer" state | "Excludes N processing docs" notice |
| Citations | Popover spinner (rare) | — | Popover/highlight | Muted/disabled badge | — | — | — | — | — |
| Research Workspace | Evidence-panel skeleton | "Select a citation" prompt | Synced 3-panel | Panel-scoped retry | Same as Ask AI (center) | — | — | — | — |
| Search | Skeleton result cards | Recent/example searches | Result list | Banner + retry | — | — | — | "No results" + suggestions | "N docs excluded (processing)" |
| Comparison | Skeleton selector | — | Diff view | Blocked at selection if doc not READY | Staged "Comparing…" | Compare button disabled until both picked | — | "0 changes detected" (positive) | "Covers first N pages" disclosure |
| Change/Conflict cards | Skeleton card | "No open conflicts" (positive) | Expanded/collapsed card | Citation-resolution fallback | — | Resolve action disabled by role | Resolve hidden for Viewer role | — | — |
| Summary | Skeleton blocks | Per-section "none found" | Full summary | Retry, stale summary preserved | "Regenerating…" banner over stale content | — | — | — | "Based on first N pages" |
| Analytics | Skeleton KPIs/charts | "Not enough activity" | Charts + KPIs | Per-widget retry | — | — | Section hidden/denied for non-admins | — | — |
| Settings | Skeleton form/table | "Invite your team" (Users) | Saved confirmation | Field/banner error, input preserved | Save-in-progress spinner | Sections disabled by role | Hidden or read-only sections | "No audit events match" | — |

---

## 19. Frontend Architecture

### 19.1 Architecture Diagram

```text
React Application
│
├── App Shell
│   ├── Sidebar
│   ├── Header (search, processing indicator, notifications, user menu)
│   ├── CommandPalette
│   └── Routing (React Router)
│
├── Pages (route-level containers — compose feature components, own URL/query-param state)
│   ├── Dashboard
│   ├── Documents            (+ Document Workspace, Upload)
│   ├── AI Chat / Ask AI      (+ Three-Panel Research Workspace)
│   ├── Search
│   ├── Compare               (+ Change Detection, Conflict Detection)
│   ├── Analytics
│   └── Settings
│
├── Feature Components (components/*)
│   ├── Document Management   (Table, Card, Header, MetadataPanel)
│   ├── Upload & Processing    (Upload, ProcessingStatus)
│   ├── Chat                   (ChatWindow, ChatMessage, ChatInput, ScopeSelector)
│   ├── Citations                (CitationBadge, SourcePreview, SourceHighlight)
│   ├── PDF Viewer                 (DocumentViewer, TableOfContents, InDocumentSearch)
│   ├── Search                      (SearchBar, SearchResultCard)
│   ├── Comparison                   (DiffViewer, ChangeCard, ComparisonSelector)
│   └── Analytics/Settings             (KpiCard, tables, forms)
│
├── State
│   ├── React Query   — all server state, cache, mutations, invalidation
│   ├── Zustand        — chat scope, panel layout, viewer state, UI preferences
│   └── Local state      — form drafts, transient open/closed UI
│
├── Realtime Layer
│   └── SSE client (useDocumentProcessingStream, chat streaming) → writes into React Query cache
│
└── API Layer
    └── Typed REST clients (per category) → FastAPI backend
```

### 19.2 Routing Ownership

- Each **Page** component owns the URL-derived state for its screen (filters, sort, page number, active tab) via query params — this makes filtered/sorted views shareable via link and survivable across refresh, and keeps `DocumentTable`/`SearchResultCard`/etc. as fully controlled, reusable components.
- Deep-linkable object routes: `/documents/:id`, `/documents/:id?version=1.0&page=12&highlight=cit_9`, `/compare/:comparisonId`, `/ask/:conversationId`, `/research/:sessionId` — all citation/search/change "open source" actions resolve to one of these URL shapes, so any evidence view is a shareable, bookmarkable link.

### 19.3 Data Flow Summary

1. Page mounts → reads URL state → issues React Query queries.
2. User interaction → either a client-state update (Zustand/local, instant) or a mutation (React Query, optimistic where justified) → cache updates → dependent components re-render.
3. Long-running server work (processing, comparison) → SSE stream → cache writes → UI reflects live progress without polling.
4. Navigation via citation/search/change-card "open source" actions → URL change → Document Workspace/viewer reads `version`/`page`/`highlight` params → renders + applies `SourceHighlight`.

---

## 20. Future UX Enhancements

Out of scope for v1, noted here so the architecture doesn't foreclose them:

1. **Inline annotation & commenting** — let reviewers leave comments anchored to a document passage (would reuse the `SourceHighlight`/bounding-box infrastructure already built for citations).
2. **Saved research sessions** — persist a Three-Panel Research Workspace session (scope + transcript + pinned evidence) as a shareable artifact for a team, not just a personal conversation.
3. **Answer diffing** — "how has the AI's answer to this question changed since the last document update" — natural extension of the comparison engine applied to Q&A rather than raw documents.
4. **Proactive conflict/change digests** — scheduled email/Slack digest of new conflicts or major changes affecting documents a user "follows."
5. **Multi-language document support** — surfaced in AI Settings ([§6.15](#615-settings)) as a placeholder already; full UI (language badges on documents, cross-language citation alignment) is a v2 concern.
6. **Custom RBAC beyond Admin/Editor/Viewer** — the `RolePermissionMatrix` component is already designed to support an arbitrary permission grid, so granular custom roles are additive, not a redesign.
7. **Bulk "ask across a collection" workflows** — templated questions run automatically across every document in a collection with results tabulated (e.g., "does this contract include a termination clause?" across 50 vendor contracts).
8. **Citation confidence exposure** — currently internal-only; a future "why this source" affordance could expose retrieval/rerank scores to power users without cluttering the default experience.

---

*End of document.*
