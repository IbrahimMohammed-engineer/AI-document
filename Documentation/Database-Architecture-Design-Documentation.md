# AI Document Intelligence Platform — Database Architecture & Design Documentation

**Document type:** Implementation-ready database architecture specification
**Audience:** Backend engineers, database engineers, DevOps, security reviewers
**Stack:** PostgreSQL (system of record) + pgvector (semantic retrieval) + Redis (infrastructure) + Object Storage (S3/Azure Blob)
**Status:** v1.0 — Draft for engineering handoff
**Companion document:** `Frontend-Design-Documentation.md`

> This document specifies the *architecture and data model*, not the implementation. It exists so a backend developer can build the persistence layer without having to make major architectural decisions independently. Migrations and application code are explicitly out of scope.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Database Architecture Overview](#2-database-architecture-overview)
3. [Storage Technologies and Their Responsibilities](#3-storage-technologies-and-their-responsibilities)
4. [Why PostgreSQL](#4-why-postgresql)
5. [Why pgvector](#5-why-pgvector)
6. [Why Redis](#6-why-redis)
7. [Why Object Storage](#7-why-object-storage)
8. [Multi-Tenant Architecture](#8-multi-tenant-architecture)
9. [Domain Model](#9-domain-model)
10. [Entity Relationship Diagram](#10-entity-relationship-diagram)
11. [Identity Schema](#11-identity-schema)
12. [Organization Schema](#12-organization-schema)
13. [Document Schema](#13-document-schema)
14. [Document Versioning](#14-document-versioning)
15. [Page and Section Model](#15-page-and-section-model)
16. [Chunking and RAG Data Model](#16-chunking-and-rag-data-model)
17. [Embedding and pgvector Design](#17-embedding-and-pgvector-design)
18. [Hybrid Search Architecture](#18-hybrid-search-architecture)
19. [Collection Model](#19-collection-model)
20. [Conversation Model](#20-conversation-model)
21. [Message Model](#21-message-model)
22. [Citation Model](#22-citation-model)
23. [Processing Job Model](#23-processing-job-model)
24. [Audit Log Model](#24-audit-log-model)
25. [Document Comparison Model](#25-document-comparison-model)
26. [Conflict Detection Model](#26-conflict-detection-model)
27. [Indexing Strategy](#27-indexing-strategy)
28. [Constraints and Referential Integrity](#28-constraints-and-referential-integrity)
29. [Soft Delete Strategy](#29-soft-delete-strategy)
30. [Data Lifecycle](#30-data-lifecycle)
31. [RAG Query Data Flow](#31-rag-query-data-flow)
32. [Document Ingestion Data Flow](#32-document-ingestion-data-flow)
33. [Document Deletion Data Flow](#33-document-deletion-data-flow)
34. [Security and Tenant Isolation](#34-security-and-tenant-isolation)
35. [Backup and Recovery](#35-backup-and-recovery)
36. [Performance Considerations](#36-performance-considerations)
37. [Scalability Strategy](#37-scalability-strategy)
38. [Observability](#38-observability)
39. [Recommended V1 Architecture](#39-recommended-v1-architecture)
40. [Future Architecture Evolution](#40-future-architecture-evolution)
41. [Architectural Decisions and Trade-offs](#41-architectural-decisions-and-trade-offs)
42. [Implementation Recommendations](#42-implementation-recommendations)

---

## 1. Executive Summary

The AI Document Intelligence Platform requires a persistence layer that does four fundamentally different jobs well: (1) store structured, relational, multi-tenant business data with strong integrity guarantees; (2) store and query high-dimensional vector embeddings for semantic retrieval; (3) store large binary files cheaply and durably; (4) provide fast, ephemeral infrastructure primitives (queues, caches, rate limits).

Rather than introducing four separate specialized databases, **V1 uses three storage technologies, not four**, because PostgreSQL can absorb both the relational and vector-retrieval responsibilities via the `pgvector` extension:

| Technology | Role |
|---|---|
| **PostgreSQL + pgvector** | System of record for all structured data *and* the semantic retrieval layer |
| **Redis** | Ephemeral infrastructure: job queue, cache, rate limiting |
| **Object Storage (S3 / Azure Blob)** | Durable binary storage for original files |

This is a deliberate simplification: a dedicated vector database (Pinecone, Qdrant, Weaviate) or a dedicated search engine (Elasticsearch/OpenSearch) would add operational surface area, a second consistency boundary, and cross-database transaction complexity that is not justified until the platform reaches a scale where PostgreSQL's vector/full-text performance genuinely becomes the bottleneck (see [§37](#37-scalability-strategy)).

Every tenant-owned row carries an `organization_id`, every document is versioned, every page/section/chunk retains provenance back to its source document and version, and every AI-generated claim is backed by a `citations` row that points to an exact chunk, page, and character span — making the entire retrieval-to-answer pipeline auditable at the database level, not just at the application level.

---

## 2. Database Architecture Overview

```text
                              AI Document Platform
                                       │
                        ┌──────────────┴──────────────┐
                        │                              │
                        ▼                              ▼
                 PostgreSQL                       Object Storage
                 + pgvector                       S3 / Azure Blob
                        │                              │
        ┌───────────────┼───────────────┐              │
        │               │               │              │
        ▼               ▼               ▼              ▼
   Identity /        Documents /     Conversations /  Original Files
   Organizations      Versions /      Messages /       Page Render
                       Chunks /       Citations         Artifacts
                       Embeddings
                        │
                        ▼
                   RAG Search
                  (vector + FTS
                   + metadata)
                        │
                        ▼
                       LLM
```

```text
                        Redis
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
             Job Queue           Cache
                 │              (search results,
                 ▼               hot documents,
              Workers            rate counters)
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
      OCR     Chunking  Embedding
```

**Responsibility summary:**

- **PostgreSQL** is the single source of truth for every fact the system needs to reconstruct — who owns what, what a document says, what version was effective when, what an AI answer cited. If PostgreSQL is lost, the platform's *knowledge* is lost (object storage alone is not enough to reconstruct structure, versions, or citations).
- **Object Storage** holds bytes. It has no concept of organizations, versions, or citations — it is addressed purely by `storage_key`, and PostgreSQL is what gives those bytes meaning.
- **Redis** holds nothing that isn't reconstructable from PostgreSQL. If Redis is flushed, in-flight jobs are re-enqueued from `processing_jobs` rows and caches simply repopulate on next read — no business fact is lost.

---

## 3. Storage Technologies and Their Responsibilities

```text
PostgreSQL
→ Persistent system of record (relational data + vector embeddings)

Redis
→ Temporary / fast-access infrastructure (queue, cache, rate limiting)

Object Storage
→ Binary file storage (original documents, rendered artifacts)
```

| Data | Belongs in |
|---|---|
| Organizations, users, roles, permissions | PostgreSQL |
| Documents, versions, pages, sections | PostgreSQL |
| Chunks + embeddings | PostgreSQL (pgvector) |
| Conversations, messages, citations | PostgreSQL |
| Processing job **metadata/history** | PostgreSQL |
| Processing job **queue/task state** | Redis |
| Audit logs | PostgreSQL |
| Original PDF/DOCX/scan files | Object Storage |
| Rendered page images (optional, perf optimization) | Object Storage |
| Search-result cache, hot-document cache | Redis |
| Rate-limit counters | Redis |
| User session tokens (if not using stateless JWT) | Redis |

A useful litmus test used throughout this document: **"If this storage layer disappeared right now, would we lose a business fact, or just lose speed?"** PostgreSQL and Object Storage answer "lose a fact." Redis should only ever answer "lose speed."

---

## 4. Why PostgreSQL

- **Mature relational integrity** — foreign keys, check constraints, transactions — is exactly what multi-tenant, versioned, hierarchical document data needs (organizations → documents → versions → sections → chunks).
- **`pgvector` eliminates a second database** for the core retrieval workload at V1 scale (see [§5](#5-why-pgvector)), meaning relational joins (organization filter, version filter, effective-date filter) and vector similarity search can run **in the same query**, in the same transaction, against the same consistent snapshot — no dual-write, no eventual-consistency gap between "the document exists" and "the document is searchable."
- **Native full-text search** (`tsvector`/`tsquery`/GIN) gives keyword search for free, enabling hybrid search without a separate search engine (see [§18](#18-hybrid-search-architecture)).
- **JSONB** covers the platform's semi-structured needs (chunk metadata, audit log payloads, comparison summaries) without schema-migration overhead for every new attribute, while keeping it queryable/indexable (GIN on JSONB).
- **Operational maturity** — mature backup/PITR tooling, managed offerings on every major cloud, well-understood scaling levers (read replicas, connection pooling, partitioning) reduce operational risk for a V1 product.

**Rejected alternative for V1:** a polyglot-persistence approach (e.g., MongoDB for documents, a graph DB for sections) was rejected — the data is fundamentally relational (strict foreign keys, versioning, referential citations), and splitting it across engines would require distributed transactions to keep, for example, a chunk's embedding consistent with its owning document's access-control state.

---

## 5. Why pgvector

`pgvector` is a PostgreSQL extension that adds a `vector` column type and approximate-nearest-neighbor (ANN) indexes (HNSW, IVFFlat) directly inside PostgreSQL.

**Why this is appropriate for V1** (explicitly, since this is the most consequential architectural decision in the document):

1. **Single consistency domain.** A chunk's embedding is written in the same transaction as the chunk row itself. There is no scenario where a chunk exists in the relational store but not in the vector store (or vice versa) — a real failure mode in dual-database RAG architectures.
2. **Metadata + vector filtering in one query.** Organization isolation, document/version filtering, and effective-date filtering are plain SQL `WHERE` clauses that execute *alongside* the ANN index scan — no need to fan out a vector-only query and then re-filter results against a separate metadata store.
3. **Sufficient performance at V1 scale.** With HNSW indexing, pgvector delivers sub-100ms ANN search up to tens of millions of vectors on adequately-sized hardware — well beyond the realistic chunk volume of an early-to-mid-stage enterprise document platform (see [§36](#36-performance-considerations) for concrete volume math).
4. **Operational simplicity.** One database to back up, monitor, secure, and scale instead of two. For a V1 product, this materially reduces time-to-market and operational risk.
5. **Clear upgrade path.** If retrieval volume or latency ever genuinely outgrows pgvector, the chunk/embedding data model documented here ([§16](#16-chunking-and-rag-data-model), [§17](#17-embedding-and-pgvector-design)) is portable — chunks and their metadata can be dual-written or backfilled into a dedicated vector database without redesigning the relational schema (see [§37](#37-scalability-strategy)).

**Rejected alternative for V1:** a dedicated vector database (Pinecone, Weaviate, Qdrant, Milvus). Rejected because it introduces a second system that must be kept consistent with PostgreSQL (a chunk's lifecycle — create, re-embed, delete — must be mirrored in two places), a second set of credentials/network paths to secure, and additional latency from cross-service calls, none of which is justified until scale or feature requirements (e.g., needing multi-region vector replication independent of the relational store) demand it.

---

## 6. Why Redis

Redis is infrastructure, not a database of record. It is used for:

- **Background job queue** — FastAPI enqueues ingestion tasks (OCR, extraction, chunking, embedding) onto Redis-backed queues (e.g., via RQ, Celery+Redis broker, or Arq); workers pull from these queues.
- **Cache** — search-result caching, hot-document metadata caching, rendered-page caching to reduce repeated PostgreSQL/object-storage round trips.
- **Rate limiting** — sliding-window or token-bucket counters per user/org for API and LLM-call throttling.
- **Short-lived state** — e.g., upload-session tokens, in-progress multi-file upload batch tracking, SSE connection fan-out coordination if running multiple API instances.

**What must NOT live permanently in Redis:** anything that is the *only* copy of a business fact — job history/audit trail (belongs in `processing_jobs`/`audit_logs` in PostgreSQL), citation data, conversation history, document metadata. Redis data must always be treated as disposable: safe to flush, safe to lose, safe to expire.

**Rejected alternative:** using PostgreSQL itself as a job queue (e.g., `SKIP LOCKED` polling tables) was considered and rejected for V1 because it would add polling load and lock contention to the primary relational database for a workload (high-frequency job claiming) that Redis is purpose-built for and PostgreSQL is not.

---

## 7. Why Object Storage

Original files (PDFs, DOCX, scanned images) must **not** be stored inside PostgreSQL as `bytea`/large objects.

```text
PostgreSQL
    │
    │ storage_key
    ▼
Azure Blob / S3
    │
    ▼
Original PDF
```

**Rationale:**

- **Cost.** Object storage is an order of magnitude cheaper per GB than provisioned database storage, and scales independently of database compute.
- **Performance isolation.** Large binary reads/writes (a 100MB scanned PDF) would bloat PostgreSQL's buffer cache, inflate backup size/time, and compete with transactional query performance if stored in-database.
- **Purpose-built features.** Object storage natively provides versioning, lifecycle policies (auto-archive/delete), signed URLs for time-limited direct client access, and multi-region replication — all without custom PostgreSQL logic.
- **Separation of concerns.** PostgreSQL stores *metadata and the pointer* (`storage_key`); object storage stores *bytes*. This mirrors the platform's own conceptual model: "documents are primary objects, described by structured metadata" (see the companion Frontend Design Documentation, [§3.2](#)).

PostgreSQL never stores file bytes — only `storage_key` (the object storage path/identifier), `mime_type`, and `file_size_bytes`. Every read of the original file goes through object storage, typically via a short-lived signed URL issued by the backend after an authorization check.

---

## 8. Multi-Tenant Architecture

```text
Organization
    │
    ├── Users
    ├── Documents (→ Versions → Pages/Sections/Chunks)
    ├── Collections
    ├── Conversations (→ Messages → Citations)
    ├── Processing Jobs (via document_versions)
    └── Audit Logs
```

**Tenant isolation model: shared database, shared schema, row-level tenancy** — every tenant-owned table carries a non-nullable `organization_id` foreign key. This is the standard, operationally simplest multi-tenancy model for a platform of this size (as opposed to database-per-tenant or schema-per-tenant, which add significant migration/operational overhead without a corresponding benefit at this scale).

**Enforcement is layered, not single-point:**

1. **Foreign keys** — every tenant-owned row's `organization_id` is a real foreign key to `organizations(id)`, not a loose string, preventing orphaned/invalid tenant references.
2. **Application-layer query scoping** — every repository/query function that reads tenant data requires an `organization_id` parameter derived from the authenticated session, never from client-supplied input; this is enforced structurally (e.g., a shared query-builder base that always injects the filter) rather than left to per-query developer discipline.
3. **PostgreSQL Row-Level Security (RLS) — recommended as a defense-in-depth enhancement**, not a V1 hard requirement, but strongly recommended given the stakes of cross-tenant document leakage:

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_documents ON documents
  USING (organization_id = current_setting('app.current_org_id')::uuid);
```

   The application sets `app.current_org_id` via `SET LOCAL` at the start of each request-scoped transaction. Even if an application-layer bug omits an `organization_id` filter, RLS makes cross-tenant access structurally impossible at the database level. This is the single highest-leverage security control available for this platform and should be adopted no later than immediately post-V1 if not in V1 itself.
4. **Vector/hybrid search queries are never allowed to omit the organization filter** — every retrieval query against `document_chunks` includes `organization_id = :org_id` (via a denormalized `organization_id` column on `document_chunks`, see [§16](#16-chunking-and-rag-data-model)) as a **hard, non-optional** predicate combined with the vector/FTS ranking, not as a post-filter applied after results are fetched — this is both a security requirement and a performance one (filtering before/during the ANN scan, not after).

**Indexing implication:** because nearly every query is implicitly scoped by `organization_id`, it is the leading column in most composite indexes throughout this schema (see [§27](#27-indexing-strategy)) — this keeps tenant-scoped scans efficient even as the platform grows to many organizations.

---

## 9. Domain Model

```text
Identity            Organizations, Users, Roles, Permissions
Organizations        (see Identity — organizations is the tenant root)
Documents             Documents, Document Versions, Pages, Sections, Chunks, Collections
AI                      Conversations, Conversation Documents, Messages, Citations
Processing               Processing Jobs
Analysis                   Document Comparisons, Comparison Changes, Conflicts, Conflict Statements
Security                    Audit Logs
```

- **Identity** answers "who can do what." **Organizations** is the tenant root every other domain hangs off. **Documents** is the core knowledge domain — the logical document, its versions, and its structural decomposition down to retrievable chunks. **AI** is the interaction layer — conversations and the messages/citations they produce, always referencing back into the Documents domain rather than duplicating content. **Processing** tracks the asynchronous pipeline that turns an uploaded file into a `READY`, queryable document version. **Analysis** covers derived, computed knowledge (comparisons, conflicts) that references but does not duplicate the Documents domain. **Security** is cross-cutting, observing every other domain.

---

## 10. Entity Relationship Diagram

```text
organizations 1───N users
organizations 1───N documents
organizations 1───N collections
organizations 1───N conversations
organizations 1───N audit_logs

roles N───M permissions        (via role_permissions)
users N───M roles              (via user_roles)

documents 1───N document_versions
documents N───1 users            (owner_id)

document_versions 1───N document_pages
document_versions 1───N document_sections
document_versions 1───N document_chunks
document_versions 1───N processing_jobs

document_sections 1───N document_sections   (self-reference: parent_section_id)
document_pages 1───N document_chunks
document_sections 1───N document_chunks

document_chunks 1───1 embedding (embedding column on the chunk itself, V1)
                              ⤷ future: document_chunks 1───N chunk_embeddings (multi-model)

collections N───M documents     (via collection_documents)

conversations N───1 users
conversations N───1 organizations
conversations N───M documents    (via conversation_documents)
conversations 1───N messages

messages 1───N citations
citations N───1 documents
citations N───1 document_versions
citations N───1 document_pages
citations N───1 document_chunks

document_comparisons N───1 document_versions (document_a_version_id)
document_comparisons N───1 document_versions (document_b_version_id)
document_comparisons 1───N comparison_changes

conflicts 1───N conflict_statements
conflict_statements N───1 document_versions
conflict_statements N───1 document_chunks
```

All relationships above are enforced with real foreign key constraints (see [§28](#28-constraints-and-referential-integrity)); no relationship in this platform is "soft" (string-matched) except where explicitly noted (none are).

---

## 11. Identity Schema

### `roles`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `organization_id` | `uuid` FK → `organizations(id)`, **nullable** | `NULL` = system-defined role (Admin/Editor/Viewer) shared across all orgs; non-null = org-defined custom role |
| `name` | `text` NOT NULL | e.g. "Admin", "Editor", "Viewer", or a custom name |
| `is_system` | `boolean` NOT NULL DEFAULT false | true for the three built-in roles, protects them from deletion |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(organization_id, name)` (with `organization_id IS NULL` handled via a partial unique index for system roles, since standard UNIQUE treats NULLs as distinct — see [§28](#28-constraints-and-referential-integrity)).

### `permissions`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `key` | `text` UNIQUE NOT NULL | e.g. `document:create`, `document:read`, `document:update`, `document:delete`, `chat:create`, `comparison:create`, `user:manage`, `settings:manage`, `analytics:read` |
| `description` | `text` | Human-readable |

Permissions are a fixed, code-owned catalog (seeded via migration), not user-editable — only their assignment to roles is configurable.

### `role_permissions` (N:M)

| Column | Type | Notes |
|---|---|---|
| `role_id` | `uuid` FK → `roles(id)` ON DELETE CASCADE | |
| `permission_id` | `uuid` FK → `permissions(id)` ON DELETE CASCADE | |

**PK:** `(role_id, permission_id)`.

### `user_roles` (N:M)

| Column | Type | Notes |
|---|---|---|
| `user_id` | `uuid` FK → `users(id)` ON DELETE CASCADE | |
| `role_id` | `uuid` FK → `roles(id)` ON DELETE CASCADE | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | Denormalized from the user for direct index-friendly tenant filtering on role lookups (avoids an extra join on every permission check, which happens on nearly every request) |

**PK:** `(user_id, role_id)`.

**Why many-to-many:** a user may hold multiple roles (e.g., "Editor" in general plus a custom "Compliance Reviewer" role), and a role's permission set is reused across every user assigned to it — changing what "Editor" can do should not require touching every editor's row individually.

---

## 12. Organization Schema

### `organizations`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `name` | `text` NOT NULL | Display name |
| `slug` | `text` UNIQUE NOT NULL | URL-safe identifier, used for SSO routing / subdomain resolution |
| `plan` | `text` NOT NULL DEFAULT `'standard'` | Subscription tier, informs quota enforcement |
| `status` | `text` NOT NULL DEFAULT `'active'` | `active` / `suspended` — suspended orgs are blocked at the auth layer, data retained |
| `settings` | `jsonb` NOT NULL DEFAULT `'{}'` | Org-level config (default AI settings, document retention policy, etc. — see Settings screen in the Frontend spec) |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Indexes:** primary key on `id`; unique index on `slug` (also serves as the lookup index for SSO/subdomain resolution).

**Relationships:** the tenant root — referenced by `users`, `documents`, `collections`, `conversations`, `audit_logs`, and (nullable) `roles`. No table above `organizations` in the tenancy hierarchy; it has no `organization_id` of its own.

`organizations` is never physically deleted from a running system in the normal product flow — organization offboarding is a data-retention/export-then-purge process governed by contract terms, handled outside the scope of this document's runtime schema.

---

## 13. Document Schema

### `documents`

Represents the **logical/business document** — not a specific file. A `documents` row is stable across every version of "Marketing Policy," while the actual content lives in `document_versions`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | Tenant owner |
| `name` | `text` NOT NULL | Display name, e.g. "Marketing Policy" |
| `description` | `text` | Optional free text |
| `document_type` | `text` NOT NULL | `policy` / `procedure` / `sop` / `contract` / `technical` / `regulatory` / `hr` / `marketing` / `other` — a constrained `text` with a `CHECK` constraint (or a lookup table `document_types` if the org needs to define custom types; V1 uses a `CHECK`-constrained enum-like text for simplicity, see [§28](#28-constraints-and-referential-integrity)) |
| `department` | `text` | Free-text or FK to a future `departments` table; V1 keeps this as `text` |
| `status` | `text` NOT NULL DEFAULT `'active'` | Document-level lifecycle: `active` / `archived` — distinct from *processing* status, which lives on `document_versions` |
| `current_version_id` | `uuid` FK → `document_versions(id)` NULL | Denormalized pointer to the current/latest effective version, kept in sync by application logic whenever a new version is published (see [§14](#14-document-versioning)); nullable because a freshly created document has no version yet |
| `owner_id` | `uuid` FK → `users(id)` NOT NULL | |
| `access_level` | `text` NOT NULL DEFAULT `'organization'` | `organization` / `restricted` / `private` — `restricted` is further scoped via a `document_permissions` table (role/user grants), out of core scope here but structurally: `document_permissions(document_id, role_id | user_id, granted_at)` |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `deleted_at` | `timestamptz` NULL | Soft delete marker, see [§29](#29-soft-delete-strategy) |

`document_tags` (N:M, separate join table rather than an array column, to keep tags queryable/indexable/normalizable):

| Column | Type |
|---|---|
| `document_id` | `uuid` FK → `documents(id)` ON DELETE CASCADE |
| `tag` | `text` |

**PK:** `(document_id, tag)`.

**Constraints:** `organization_id`, `owner_id` NOT NULL; `document_type` CHECK constraint against the allowed set; `access_level` CHECK constraint.

**Indexes:** see [§27](#27-indexing-strategy) — `organization_id`, `status`, `document_type`, `owner_id`, `created_at`, `updated_at`, and a partial index `WHERE deleted_at IS NULL` for the common "active documents" query shape.

**Why documents/versions are separate:** a business document's *identity* (its name, owner, department, access rules, tags) is stable across revisions, but its *content* changes — this split is what lets the platform show "Marketing Policy" as one row in the Documents table with a version history, rather than three unrelated rows for 2025/2026/2027 that happen to share a name.

---

## 14. Document Versioning

### `document_versions`

```text
Marketing Policy (documents.id = doc_1)
    │
    ├── v1 — 2025  (effective 2025-01-01, expired 2025-12-31)
    ├── v2 — 2026  (effective 2026-01-01, current)
    └── v3 — 2027  (draft, not yet effective)
```

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE CASCADE NOT NULL | |
| `version_number` | `integer` NOT NULL | Monotonically increasing per `document_id` (1, 2, 3…) — application-assigned, not user-editable |
| `version_label` | `text` | Human-facing label, e.g. `"2026"`, `"v2.1"` |
| `effective_date` | `date` NULL | When this version's content takes/took effect |
| `expiration_date` | `date` NULL | When this version stopped being effective (NULL = still effective or not yet determined) |
| `storage_key` | `text` NOT NULL | Object storage path to the original file for this specific version |
| `mime_type` | `text` NOT NULL | |
| `file_size_bytes` | `bigint` NOT NULL | |
| `status` | `text` NOT NULL DEFAULT `'UPLOADED'` | Pipeline status: `UPLOADED` / `PROCESSING` / `EXTRACTING` / `OCR` / `EMBEDDING` / `INDEXING` / `READY` / `FAILED` |
| `error_message` | `text` NULL | Populated when `status = 'FAILED'` |
| `page_count` | `integer` NULL | Populated after extraction |
| `created_by` | `uuid` FK → `users(id)` NOT NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(document_id, version_number)`.

**Current-version mechanism:** `documents.current_version_id` is the single source of truth for "which version is current" (rather than an `is_current` boolean on `document_versions`, which would require a database trigger or careful application logic to guarantee exactly one `true` row per document — a classic source of data-integrity bugs). Application logic updates `documents.current_version_id` transactionally whenever a new version reaches `READY` status *and* its `effective_date` is the most recent non-future effective date for that document. This single-pointer design also makes "get me the current version" a zero-join, index-friendly lookup from the `documents` row itself.

**Effective-date semantics — critical for both RAG and comparison:**
- **"Current" for retrieval purposes** = the version with the latest `effective_date <= today` and (`expiration_date IS NULL OR expiration_date >= today`).
- Multiple versions can technically have overlapping or missing effective dates (e.g., a regional addendum with its own effective window) — this is precisely the scenario the [Conflict Detection Model](#26-conflict-detection-model) exists to surface, rather than something the versioning model tries to silently resolve.
- **Version-aware RAG:** by default, retrieval queries join `document_chunks` → `document_versions` and filter to `document_versions.id = documents.current_version_id` (i.e., only the current version's chunks are searched) unless the user's scope explicitly includes older versions (e.g., "compare 2025 vs 2026" intentionally targets non-current versions).
- **Document comparison** ([§25](#25-document-comparison-model)) operates directly on two `document_versions` rows, which is exactly why versions — not just documents — are the unit comparison operates on.

**Historical versions are never deleted** as part of normal operation (only via the explicit deletion flow in [§33](#33-document-deletion-data-flow)); they remain queryable for audit, comparison, and historical-citation-verification purposes indefinitely, governed by the org's retention policy.

---

## 15. Page and Section Model

### `document_pages`

Pages are **first-class entities**, not just a rendering concern, because nearly every explainability feature in the product depends on a stable page identity: citations point to a page, source previews render a page, comparison anchors changes to a page, and the PDF viewer navigates by page.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE CASCADE NOT NULL | |
| `page_number` | `integer` NOT NULL | 1-indexed |
| `text` | `text` NOT NULL | Extracted (or OCR'd) raw text for the page — used for in-document search fallback and as citation context |
| `ocr_used` | `boolean` NOT NULL DEFAULT false | True if this page's text came from OCR rather than native extraction |
| `render_storage_key` | `text` NULL | Optional pointer to a pre-rendered page image/asset in object storage (perf optimization for the viewer — avoids re-rasterizing PDF pages server-side on every view) |
| `width` | `numeric` NULL | Page dimensions, used to normalize bounding-box coordinates for highlight overlays |
| `height` | `numeric` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(document_version_id, page_number)`.

### `document_sections`

Supports hierarchical table-of-contents structure via a self-referencing foreign key:

```text
4 Approval Process
│
├── 4.1 Marketing Review
├── 4.2 Regulatory Review
└── 4.3 Medical Review
```

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE CASCADE NOT NULL | |
| `parent_section_id` | `uuid` FK → `document_sections(id)` ON DELETE CASCADE NULL | Self-reference; `NULL` = top-level section |
| `title` | `text` NOT NULL | |
| `section_number` | `text` NULL | e.g. `"4.2"` — kept as `text` (not numeric) since section numbering schemes vary (`4.2`, `IV.b`, `Appendix A`) |
| `start_page` | `integer` NOT NULL | |
| `end_page` | `integer` NULL | |
| `sort_order` | `integer` NOT NULL | Sibling ordering (section numbers alone aren't always sortable as strings) |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Why self-referencing `parent_section_id` rather than a materialized path or a fixed-depth schema (`section`, `subsection`, `subsubsection` columns):** table-of-contents depth is not bounded in real business documents (some SOPs nest 4–5 levels deep), and a self-referencing adjacency list is the simplest model that supports arbitrary depth while remaining trivial to query for "immediate children" (the dominant access pattern, used to render the TOC tree). Recursive `WITH RECURSIVE` queries are used for the rarer "full ancestor path" need (e.g., rendering a citation's breadcrumb "§4 → §4.2").

**Indexes:** `(document_version_id, parent_section_id)` — supports both "all sections for this version" and "children of this section" access patterns with one composite index.

---

## 16. Chunking and RAG Data Model

### `document_chunks`

The retrieval unit. Each chunk retains full provenance back to its page and section, which is what makes citations precise rather than approximate.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | **Denormalized** from the owning document, deliberately — every retrieval query filters on this column directly, and requiring a join through `document_versions → documents` on every RAG query would be both a performance cost and a tenant-isolation risk surface (see [§8](#8-multi-tenant-architecture)) |
| `document_version_id` | `uuid` FK → `document_versions(id)` ON DELETE CASCADE NOT NULL | |
| `page_id` | `uuid` FK → `document_pages(id)` ON DELETE CASCADE NOT NULL | The chunk's starting page |
| `end_page_id` | `uuid` FK → `document_pages(id)` NULL | Populated only if a chunk spans multiple pages |
| `section_id` | `uuid` FK → `document_sections(id)` ON DELETE SET NULL NULL | Nullable — some content (preambles, unstructured docs) has no section |
| `chunk_index` | `integer` NOT NULL | Sequential order within the document version, `0`-based |
| `content` | `text` NOT NULL | The chunk's text |
| `content_hash` | `text` NOT NULL | SHA-256 of `content`, used for dedup detection and cheap change-detection between versions before running full semantic diff |
| `token_count` | `integer` NOT NULL | Token count for the configured tokenizer — used for LLM context-budget accounting |
| `metadata` | `jsonb` NOT NULL DEFAULT `'{}'` | Structural extras: heading path, bounding boxes for highlight rendering, table/figure flags |
| `embedding` | `vector(1536)` NULL | See [§17](#17-embedding-and-pgvector-design) — nullable until the embedding step completes |
| `embedding_model` | `text` NULL | e.g. `"text-embedding-3-small@1"` — records exactly which model/version produced the vector, critical for [§17](#17-embedding-and-pgvector-design)'s re-indexing/versioning story |
| `content_tsv` | `tsvector` GENERATED ALWAYS AS (`to_tsvector('english', content)`) STORED | Full-text search vector, see [§18](#18-hybrid-search-architecture) |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(document_version_id, chunk_index)`.

**Chunking strategy (recorded here for schema rationale, governed by the ingestion pipeline, not the schema itself):** chunks are produced per document version at ingestion time using a structure-aware splitter — section boundaries are preferred split points, with a target size (e.g., ~500–800 tokens) and a small overlap between adjacent chunks to avoid severing a sentence that matters for retrieval. `chunk_index` preserves reading order, which the citation UI and "expand context" features rely on to fetch neighboring chunks.

**Chunk ↔ page relationship:** every chunk knows its exact page(s) — this is what lets a citation say "page 12" with certainty rather than an estimate.

**Chunk ↔ section relationship:** nullable by design — a chunk always belongs to exactly one page but not every document has detected structure, and forcing a non-null section would either require a synthetic root section (adds noise) or block ingestion of unstructured documents (unacceptable, since scanned/legacy documents are an explicit product target).

**Why the embedding lives directly on the chunk (V1), not in a separate table:** at V1, there is exactly one embedding per chunk (one model, one version in production at a time), so a separate `chunk_embeddings` table would be a 1:1 relationship with no query benefit — only extra join overhead on every retrieval query, which is the platform's hottest read path. **A dedicated `chunk_embeddings` table should be introduced when the platform needs to support multiple embedding models or model versions simultaneously** (e.g., A/B testing a new embedding model, or supporting per-organization model choice) — at that point the relationship becomes genuinely 1:N and the join cost is justified by the flexibility gained:

```sql
-- Future extension, not part of V1:
CREATE TABLE chunk_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chunk_id uuid NOT NULL REFERENCES document_chunks(id) ON DELETE CASCADE,
  embedding_model text NOT NULL,
  embedding vector(1536) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chunk_id, embedding_model)
);
```

**Indexes:** see [§27](#27-indexing-strategy) — `document_version_id`, `page_id`, `section_id`, the HNSW index on `embedding`, and a GIN index on `content_tsv`.

---

## 17. Embedding and pgvector Design

```text
Chunk
 ↓
Embedding Model (text-embedding-3-small)
 ↓
Vector (1536 dimensions)
 ↓
PostgreSQL pgvector column
```

```sql
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE document_chunks
  ADD COLUMN embedding vector(1536);
```

**Vector dimensionality:** the column is sized to the platform's default embedding model. This document specifies **1536 dimensions**, matching a standard modern general-purpose embedding model (e.g., OpenAI `text-embedding-3-small`); the actual model is a configuration choice, not a schema choice, but **`pgvector` requires the column's dimensionality to be fixed at creation time and match every vector written to it** — the implementation team must pin the production embedding model *before* running the migration that creates this column, and record that decision (model name + dimension) in `embedding_model` on every row, since changing embedding models later requires either a new column sized for the new dimension or the `chunk_embeddings` extension table above (a straight `ALTER COLUMN` to a different vector dimension is not possible — the column must be re-created and every chunk re-embedded).

**Similarity metric:** **cosine similarity** (`vector_cosine_ops`), the standard choice for text embeddings from modern models, which are typically trained/normalized for cosine comparison. pgvector also supports L2 (`vector_l2_ops`) and inner product (`vector_ip_ops`); cosine is specified here as the platform default unless the chosen embedding model's documentation recommends otherwise.

**ANN index — HNSW recommended over IVFFlat for V1:**

```sql
CREATE INDEX document_chunks_embedding_hnsw_idx
  ON document_chunks
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

| Consideration | HNSW | IVFFlat |
|---|---|---|
| Build-time table-size knowledge | Not required | Requires an estimate of row count to pick `lists` well |
| Recall/query performance | Generally higher recall at comparable latency | Requires tuning `probes` at query time to trade recall for speed |
| Behavior as data grows | Index remains effective; no forced rebuild | Degrades in recall as data grows past the original `lists` estimate, often needs periodic re-`lists`/rebuild |
| Build cost | Higher build/insert cost | Lower build cost |
| **V1 recommendation** | **Use** — a growing, incrementally-ingested corpus (documents uploaded continuously, not bulk-loaded once) is exactly the scenario HNSW handles better without manual re-tuning | Reasonable only if the corpus size were known and largely static upfront, which does not match this product's usage pattern |

Query-time recall/speed is tuned via `SET hnsw.ef_search = 100;` (session/transaction-scoped), adjustable without rebuilding the index — set higher for Ask AI (favor recall) and can be left lower for latency-sensitive auto-suggest style features if introduced later.

**Metadata filtering alongside vector search:** the platform never runs a "pure" vector query — every retrieval query combines the ANN search with relational predicates in one statement:

```sql
SELECT c.id, c.content, c.page_id, c.section_id,
       1 - (c.embedding <=> :query_embedding) AS similarity
FROM document_chunks c
JOIN document_versions v ON v.id = c.document_version_id
WHERE c.organization_id = :org_id
  AND v.document_id = ANY(:allowed_document_ids)
  AND v.id = :current_version_id_for_each_document   -- or explicit version scope
  AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> :query_embedding
LIMIT 20;
```

PostgreSQL's query planner can use the relational filters to restrict the candidate set the HNSW scan considers when selectivity is high; for typical per-organization/per-document scopes this performs well in practice, and is continuously validated via `EXPLAIN ANALYZE` as part of standard query review (see [§36](#36-performance-considerations)).

**Re-indexing strategy:** the HNSW index updates incrementally as rows are inserted (no manual rebuild needed for normal ingestion). A full `REINDEX` is only operationally necessary after a bulk re-embedding event (e.g., migrating the whole corpus to a new embedding model), and should be run `CONCURRENTLY` where supported to avoid blocking reads.

**Embedding model versioning:** `document_chunks.embedding_model` records provenance per row. A platform-wide "active embedding model" is a single config value; when it changes, **existing chunks are not silently mixed with new-model vectors in the same similarity comparison** — the ingestion/re-embedding job re-embeds affected chunks in a background job and only flips a document version's chunks to "searchable" once fully re-embedded, preventing a mixed-model corpus from producing meaningless cross-model similarity scores.

---

## 18. Hybrid Search Architecture

```text
Vector Search (pgvector, semantic)
        +
PostgreSQL Full-Text Search (tsvector, keyword)
        +
Metadata Filtering (organization, document type, version/effective date)
```

**Full-text search setup:**

```sql
-- content_tsv is a generated column on document_chunks (see §16)
CREATE INDEX document_chunks_content_tsv_idx
  ON document_chunks
  USING GIN (content_tsv);
```

```sql
SELECT id, content, ts_rank(content_tsv, query) AS rank
FROM document_chunks, plainto_tsquery('english', :search_text) query
WHERE content_tsv @@ query
  AND organization_id = :org_id
ORDER BY rank DESC
LIMIT 20;
```

**Combining semantic and keyword results — Reciprocal Rank Fusion (RRF):** PostgreSQL has no built-in RRF, so it is computed in SQL (or equivalently in the application layer over two result sets) by ranking each method's results independently and merging by the RRF formula `score = Σ 1 / (k + rank)` (typically `k = 60`):

```sql
WITH semantic AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> :query_embedding) AS rank
  FROM document_chunks
  WHERE organization_id = :org_id AND embedding IS NOT NULL
  ORDER BY embedding <=> :query_embedding
  LIMIT 50
),
keyword AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(content_tsv, query) DESC) AS rank
  FROM document_chunks, plainto_tsquery('english', :search_text) query
  WHERE organization_id = :org_id AND content_tsv @@ query
  ORDER BY ts_rank(content_tsv, query) DESC
  LIMIT 50
)
SELECT COALESCE(s.id, k.id) AS chunk_id,
       COALESCE(1.0 / (60 + s.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS rrf_score
FROM semantic s
FULL OUTER JOIN keyword k ON s.id = k.id
ORDER BY rrf_score DESC
LIMIT 20;
```

The resulting top-N chunk IDs are then passed to the (out-of-scope-for-this-document) **reranking** step, which may use a cross-encoder model for final ordering before being sent to the LLM as context.

**Example — "What was the approval process in 2025?"** — the query pipeline combines:
1. **Semantic similarity** on the query embedding against `document_chunks.embedding`.
2. **`document_type` / metadata filter** if the query or scope implies one (not applicable in this example, but available).
3. **Version/effective-date filter** — the phrase "in 2025" is resolved (at the application/retrieval-orchestration layer, not the database layer) to target the `document_versions` row whose `effective_date` covers 2025, rather than the current version — this is precisely why `document_versions` carries its own `effective_date` rather than only `documents` carrying a single "current" pointer.
4. **`organization_id` filter** — always present, never optional.

**Indexing to support this:** the HNSW index ([§17](#17-embedding-and-pgvector-design)) and the GIN `tsvector` index (above) are the two workhorses; both are most effective when the relational filters (`organization_id`, `document_version_id`) are also indexed (see [§27](#27-indexing-strategy)) so the planner can intersect efficiently rather than sequentially scanning a large filtered set before ranking.

---

## 19. Collection Model

### `collections`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | |
| `name` | `text` NOT NULL | e.g. "HR Policies," "Vendor Contracts" |
| `description` | `text` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(organization_id, name)`.

### `collection_documents` (N:M)

| Column | Type | Notes |
|---|---|---|
| `collection_id` | `uuid` FK → `collections(id)` ON DELETE CASCADE | |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE CASCADE | |
| `added_at` | `timestamptz` NOT NULL DEFAULT now() | |

**PK:** `(collection_id, document_id)`.

**Why many-to-many:** a vendor contract, for example, may reasonably belong to both a "Legal" collection and a "Vendor Management" collection — collections are an organizing/filtering construct (mirrored directly by the frontend's Collection filter, see the companion Frontend spec §6.3), not an exclusive folder hierarchy.

---

## 20. Conversation Model

### `conversations`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | |
| `user_id` | `uuid` FK → `users(id)` NOT NULL | Owner — conversations are private to the creating user in V1 (no shared/team conversations yet, see [§40](#40-future-architecture-evolution)) |
| `title` | `text` NULL | Auto-generated from the first message if not user-set |
| `scope_type` | `text` NOT NULL DEFAULT `'knowledge_base'` | `current_document` / `selected_documents` / `knowledge_base` — mirrors the frontend's scope selector directly |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` NOT NULL DEFAULT now() | Bumped on every new message, used to order the conversation list by recency |
| `deleted_at` | `timestamptz` NULL | Soft delete — "archive conversation" |

**Lifecycle:** created on the first message send (not on merely opening the Ask AI screen — an empty conversation with no messages is not persisted, avoiding clutter in the conversation list). `scope_type` can change mid-conversation (per the frontend spec's "scope changed" system marker) — this is recorded as a synthetic `SYSTEM`-role message (see [§21](#21-message-model)) rather than mutating `conversations.scope_type` silently, preserving the exact scope that was active for each historical message.

---

## 21. Conversation Documents

### `conversation_documents`

Records the retrieval scope for a conversation — which documents were selected when `scope_type = 'selected_documents'`.

| Column | Type | Notes |
|---|---|---|
| `conversation_id` | `uuid` FK → `conversations(id)` ON DELETE CASCADE | |
| `document_id` | `uuid` FK → `documents(id)` ON DELETE CASCADE | |
| `added_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `removed_at` | `timestamptz` NULL | Set (not deleted) when a document is unchecked mid-conversation |

**PK:** `(conversation_id, document_id)`.

**Why this matters for reproducibility:** without this table, "why did the AI answer this way" is unanswerable after the fact if the user later changes their document selection — the scope at the time of each message must be reconstructable. Combined with the per-message `SYSTEM` scope-change markers ([§20](#20-conversation-model)), the exact retrieval scope for any historical message can always be reconstructed, which is essential both for user trust (the frontend can show "this answer used these 2 documents") and for debugging/QA of retrieval quality.

---

## 22. Message Model

### `messages`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `conversation_id` | `uuid` FK → `conversations(id)` ON DELETE CASCADE NOT NULL | |
| `role` | `text` NOT NULL | `USER` / `ASSISTANT` / `SYSTEM` — CHECK constraint |
| `content` | `text` NOT NULL | |
| `model` | `text` NULL | LLM model/version used — **assistant messages only** |
| `prompt_tokens` | `integer` NULL | **Assistant messages only** — for cost/usage analytics |
| `completion_tokens` | `integer` NULL | **Assistant messages only** |
| `retrieval_ms` | `integer` NULL | Time spent in retrieval (vector+FTS+rerank) — **assistant messages only**, feeds the Analytics latency breakdown |
| `latency_ms` | `integer` NULL | Total end-to-end response time — **assistant messages only** |
| `groundedness` | `text` NULL | `grounded` / `partial` / `ungrounded` — **assistant messages only**, drives the frontend's "no grounded answer found" state and the Analytics "Grounded Answers %" metric |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Which fields are necessary vs. optional:** `id`, `conversation_id`, `role`, `content`, `created_at` are required for every message, including `USER` and `SYSTEM` roles. `model`, `prompt_tokens`, `completion_tokens`, `retrieval_ms`, `latency_ms`, `groundedness` are meaningful only for `ASSISTANT` messages and are `NULL` otherwise — modeled as nullable columns on one table rather than a separate `assistant_message_details` table, since the 1:1 relationship and the modest column count don't justify the join overhead on every conversation read (the dominant read pattern is "give me all messages for this conversation," where a wide nullable-column table is simpler and faster than a join).

### `message_feedback`

Kept as a separate table rather than columns on `messages`, because feedback has its own actor and timestamp independent of the message's author, and — in principle — more than one reviewer could rate the same answer (e.g., a QA reviewer auditing AI quality separately from the end user's thumbs-up/down).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `message_id` | `uuid` FK → `messages(id)` ON DELETE CASCADE NOT NULL | |
| `user_id` | `uuid` FK → `users(id)` NOT NULL | |
| `rating` | `smallint` NOT NULL | `-1` (negative) / `1` (positive) — CHECK constraint |
| `comment` | `text` NULL | Optional reason, captured on negative feedback in the frontend |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Unique:** `(message_id, user_id)` — one rating per user per message (a resubmission updates the existing row rather than inserting a duplicate).

---

## 23. Citation Model

### `citations`

The single most important table for the platform's explainability principle — every AI-generated claim traces back through this table to exact source evidence.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `message_id` | `uuid` FK → `messages(id)` ON DELETE CASCADE NOT NULL | Always an `ASSISTANT` message |
| `citation_index` | `integer` NOT NULL | The `[1]`/`[2]` ordinal as it appears inline in the message |
| `document_id` | `uuid` FK → `documents(id)` NOT NULL | Denormalized for direct joins without traversing `document_versions` |
| `document_version_id` | `uuid` FK → `document_versions(id)` NOT NULL | |
| `chunk_id` | `uuid` FK → `document_chunks(id)` NOT NULL | The exact retrieved chunk this citation is grounded in |
| `page_id` | `uuid` FK → `document_pages(id)` NOT NULL | |
| `page_number` | `integer` NOT NULL | Denormalized from `document_pages` for zero-join rendering in the citation badge/list UI |
| `section` | `text` NULL | Denormalized section title/path, e.g. `"4.2 Regulatory Review"` |
| `quoted_text` | `text` NOT NULL | The exact source span the claim is based on |
| `char_start` | `integer` NULL | Offset of `quoted_text` within the page/chunk text, used to drive the frontend's exact-highlight overlay |
| `char_end` | `integer` NULL | |
| `relevance_score` | `numeric(6,5)` NULL | The retrieval score (similarity/RRF/rerank) for this chunk at generation time — internal-facing primarily, optionally surfaced to power users |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

```text
Message
   │
   └── Citation
         │
         ├── Document          (document_id)
         ├── Document Version  (document_version_id)
         ├── Page               (page_id, page_number)
         └── Chunk                (chunk_id, quoted_text, char_start/char_end)
```

**Why citations are denormalized this heavily (`document_id`, `page_number`, `section` duplicated from their normalized source tables):** citations are read on the hottest, most latency-sensitive path in the product — rendering every AI answer, hovering every badge. Denormalizing avoids a 4-table join (`citations → document_chunks → document_pages/document_sections → document_versions → documents`) on every message render; the normalized foreign keys are retained for integrity and for the rare case that needs to re-resolve (e.g., "has this citation's source document been deleted since").

**What this enables directly, table by table:**
- **Citation badges** — `citation_index` + `document_id` render the `[1] Document Name` inline marker without any join.
- **Source previews** — `quoted_text` + `char_start`/`char_end` render the exact highlighted span in its immediate context.
- **Exact page navigation** — `page_number` (plus `document_version_id` to select the right file) is everything the frontend viewer needs to jump straight to the right page.
- **Source text highlighting** — `chunk_id` → `document_chunks.metadata` (bounding box, if extracted) drives the visual overlay.
- **Explainable AI** — the full chain `message → citation → chunk → page → section → document → version` is always walkable in both directions: "what supports this answer" (forward) and "what did this passage ever get cited for" (backward, useful for audit — see `resource_id` usage in [§24](#24-audit-log-model)).
- **Citation validation** — because `chunk_id` is a real foreign key (not a copied string), it is structurally impossible for a citation to reference content that was never actually retrieved/chunked; a citation can only ever point at something that genuinely exists in the corpus.

---

## 24. Audit Log Model

### `audit_logs`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | |
| `user_id` | `uuid` FK → `users(id)` NULL | Nullable for system-initiated actions (e.g., an automated retention-policy deletion) |
| `action` | `text` NOT NULL | e.g. `USER_LOGIN`, `DOCUMENT_UPLOADED`, `DOCUMENT_VIEWED`, `DOCUMENT_DELETED`, `QUESTION_ASKED`, `DOCUMENT_COMPARED`, `ACCESS_LEVEL_CHANGED`, `USER_ROLE_CHANGED` |
| `resource_type` | `text` NOT NULL | e.g. `document`, `conversation`, `user`, `comparison` |
| `resource_id` | `uuid` NULL | Intentionally **not** a foreign key (see below) |
| `metadata` | `jsonb` NOT NULL DEFAULT `'{}'` | Action-specific context, e.g. `{"previous_access_level": "organization", "new_access_level": "private"}` |
| `ip_address` | `inet` NULL | |
| `user_agent` | `text` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Why `resource_id` is deliberately not a foreign key:** an audit log must remain a truthful historical record even after the resource it refers to has been permanently deleted (e.g., `DOCUMENT_DELETED` followed by eventual hard-deletion of the row after the retention window). A foreign key would force `ON DELETE SET NULL` or `CASCADE`, either silently corrupting the audit trail (losing which resource) or deleting audit history alongside the resource — both unacceptable for a compliance-oriented feature. `resource_type` + `resource_id` is an intentional "soft reference," the one deliberate exception to this document's "every relationship is a real foreign key" rule ([§10](#10-entity-relationship-diagram)), justified specifically by audit-log semantics.

**Why audit logs matter for this product specifically:** the platform's entire value proposition is trustworthy, explainable handling of compliance-sensitive documents (policies, regulatory filings, contracts) — an enterprise customer's own compliance/security review will expect to see who viewed, changed, deleted, or asked questions about sensitive documents, and when. `QUESTION_ASKED` and `DOCUMENT_COMPARED` entries additionally support reconstructing "what did users learn from this document" for compliance investigations.

**Immutability:** `audit_logs` rows are insert-only at the application/database-role level — no `UPDATE` or `DELETE` grants are given to the application's runtime database role for this table (enforced via `REVOKE UPDATE, DELETE ON audit_logs FROM app_user;`), so even a compromised application cannot rewrite history; only a dedicated retention-purge process with elevated privileges may delete rows older than the org's retention policy.

**Scale consideration:** `audit_logs` is expected to be the highest-volume table in the schema (every view, every question, every action). See [§27](#27-indexing-strategy) and [§36](#36-performance-considerations) for partitioning guidance.

---

## 25. Document Comparison Model

**Recommendation: comparisons are computed on demand but their results are persisted, not merely cached.**

**Reasoning — three options considered:**

| Option | Verdict |
|---|---|
| **Generate on every request, never store** | Rejected — comparison (diffing two full document versions, classifying severity) is compute-heavy enough that re-running it every time a user revisits a comparison view is wasteful, and it forfeits any audit trail of "what did the comparison show at the time it was reviewed." |
| **Cache only (e.g., Redis, TTL-based)** | Rejected as the sole store — comparisons are deterministic given the same two versions and the same comparison-model version, so they are exactly the kind of durable, referenceable fact this document's PostgreSQL-as-system-of-record principle applies to; a TTL-evicted cache would silently regenerate (and potentially return a slightly different result if the underlying comparison logic changed) rather than serving a stable, citable prior result. |
| **Persist in a dedicated `document_comparisons` / `comparison_changes` domain (chosen)** | A comparison, once computed for a given version pair, is reused for every future view of that pair (enforced via a uniqueness constraint), is directly linkable (`/compare/:comparisonId`, matching the frontend spec), and forms part of the audit trail ("this comparison was reviewed and this conflict was marked resolved based on it"). |

### `document_comparisons`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | |
| `document_a_version_id` | `uuid` FK → `document_versions(id)` NOT NULL | |
| `document_b_version_id` | `uuid` FK → `document_versions(id)` NOT NULL | |
| `status` | `text` NOT NULL DEFAULT `'PENDING'` | `PENDING` / `PROCESSING` / `COMPLETED` / `FAILED` |
| `summary` | `jsonb` NULL | `{"total": 12, "major": 4, "moderate": 5, "minor": 3}` — populated on completion |
| `requested_by` | `uuid` FK → `users(id)` NOT NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |
| `completed_at` | `timestamptz` NULL | |

**Unique:** `(document_a_version_id, document_b_version_id)` — enforces reuse: requesting the same comparison twice returns the existing row rather than recomputing (order-normalized at the application layer, e.g., always storing the lower `version_number`'s version as "A," so A-vs-B and B-vs-A resolve to the same row).

### `comparison_changes`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `comparison_id` | `uuid` FK → `document_comparisons(id)` ON DELETE CASCADE NOT NULL | |
| `change_type` | `text` NOT NULL | `ADDED` / `REMOVED` / `MODIFIED` — CHECK constraint |
| `severity` | `text` NOT NULL | `MAJOR` / `MODERATE` / `MINOR` — CHECK constraint |
| `section` | `text` NULL | Denormalized section path for display |
| `old_chunk_id` | `uuid` FK → `document_chunks(id)` NULL | NULL for `ADDED` |
| `new_chunk_id` | `uuid` FK → `document_chunks(id)` NULL | NULL for `REMOVED` |
| `old_text` | `text` NULL | Denormalized snapshot — see below |
| `new_text` | `text` NULL | |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**Why `old_text`/`new_text` are denormalized snapshots rather than resolved live from `old_chunk_id`/`new_chunk_id` at read time:** if a source document version is ever hard-deleted (post-retention-window, [§29](#29-soft-delete-strategy)) but the comparison record is retained for audit purposes, the comparison must still be able to display what the change *was* — the chunk foreign keys are nullable on delete for exactly this reason (`ON DELETE SET NULL`, not `CASCADE`, since a comparison should outlive the chunks it was computed from once persisted as a historical record).

**Indexes:** `(comparison_id, severity)` supports the frontend's severity-filtered changes list directly.

---

## 26. Conflict Detection Model

**Recommendation: persisted in dedicated tables, not generated dynamically.**

**Reasoning:** unlike a comparison (explicitly requested by a user, between two known documents), conflict detection is a **system-initiated background scan** across the corpus that must support a stateful **review/resolution workflow** ("Mark as Reviewed," "Escalate," per the frontend spec §6.13) — a dynamically-generated, non-persisted conflict has nowhere to record that a human already reviewed and dismissed it, which would mean the same conflict resurfaces every time the scan reruns. Persistence is not optional here; it's required by the feature's own workflow semantics.

### `conflicts`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `organization_id` | `uuid` FK → `organizations(id)` NOT NULL | |
| `topic` | `text` NOT NULL | Short label grouping the conflicting statements, e.g. `"Approval Timeline Requirement"` |
| `severity` | `text` NOT NULL | `MAJOR` / `MODERATE` / `MINOR` |
| `status` | `text` NOT NULL DEFAULT `'OPEN'` | `OPEN` / `REVIEWED` / `DISMISSED` |
| `detection_method` | `text` NOT NULL | `background_scan` / `comparison_derived` / `retrieval_time` — records how the conflict was found, useful for tuning detection quality over time |
| `resolved_by` | `uuid` FK → `users(id)` NULL | |
| `resolved_at` | `timestamptz` NULL | |
| `resolution_note` | `text` NULL | |
| `detected_at` | `timestamptz` NOT NULL DEFAULT now() | |

### `conflict_statements`

A conflict is modeled as **N conflicting statements**, not a hardcoded pair, because more than two documents can disagree on the same point (e.g., a global policy, a regional addendum, and an outdated local memo all specifying different approval windows).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `conflict_id` | `uuid` FK → `conflicts(id)` ON DELETE CASCADE NOT NULL | |
| `document_version_id` | `uuid` FK → `document_versions(id)` NOT NULL | |
| `chunk_id` | `uuid` FK → `document_chunks(id)` NOT NULL | |
| `statement_text` | `text` NOT NULL | |
| `effective_date` | `date` NULL | Denormalized from the version for quick chronological sort in the UI (critical for the "is this actually still a conflict or just a superseded document" judgment call the frontend surfaces) |
| `created_at` | `timestamptz` NOT NULL DEFAULT now() | |

**How the pieces work together to represent a conflict** (directly answering the source requirement):
- **Document versions** give each statement a concrete origin and, via `effective_date`, a timeline position.
- **Effective dates** let both the backend (detection logic) and frontend (display logic) distinguish a genuine open conflict (two currently-effective documents disagreeing) from a stale artifact (one side is a superseded, non-current version) — the frontend's "Likely resolved by version update" hint (Frontend spec §6.13) is computed directly from comparing each statement's `document_version_id` against its document's `current_version_id`.
- **Chunks** anchor each statement to exact source text, so `conflict_statements` reuses the same citation-grade precision as the `citations` table rather than inventing a parallel evidence model.
- **Citations** are not directly foreign-keyed from conflicts (a conflict may be surfaced independent of any conversation), but the "Compare Sources" action in the frontend reuses the `document_comparisons` machinery ([§25](#25-document-comparison-model)) by opening a comparison scoped to the two statements' document versions, section-anchored — the conflict and comparison domains intentionally share their visual and data language rather than duplicating diff logic.
- **Comparison results** can be one *source* of detected conflicts (`detection_method = 'comparison_derived'`) — when a comparison surfaces a `MODIFIED` change to a factual/numeric statement across non-superseded versions, that can programmatically seed a `conflicts` row — but conflicts can also be detected independent of any user-run comparison (background corpus-wide scan), which is why `conflicts` is its own domain rather than a view over `comparison_changes`.

---

## 27. Indexing Strategy

Every index below is listed with its reasoning, not just its columns — an index without a stated query pattern it serves is a maintenance liability (write overhead with no read benefit).

### Organizations
- **PK** `id` — direct lookup by every tenant-scoped query's join target.
- **Unique** `slug` — SSO/subdomain resolution at login time, before the user's `organization_id` is otherwise known.

### Users
- `(organization_id, email)` UNIQUE — supports both "is this email already in this org" validation and org-scoped login lookup in one index.
- `organization_id` (covered by the composite above) — "list users in this org" (Settings → Users).

### Documents
- `organization_id` — every Documents-page query starts here; leading column since it's the universal filter.
- `(organization_id, status)` — the Documents table's default "active documents" view.
- `(organization_id, document_type)` — Documents-page type filter.
- `owner_id` — "my documents" filtering, and cascading-impact checks when a user is deactivated.
- `created_at`, `updated_at` — Dashboard "recently uploaded," Documents table default sort.
- **Partial index** `(organization_id) WHERE deleted_at IS NULL` — the overwhelmingly common query shape ("active, non-deleted documents") gets a smaller, faster-to-scan index than indexing every soft-deleted row too.

### Document Versions
- `(document_id, effective_date)` — resolving "the version effective on date X" ([§18](#18-hybrid-search-architecture) example).
- `(document_id, version_number)` UNIQUE — also serves "get all versions for this document in order."
- `status` — the Dashboard/header processing-activity widgets query "all non-`READY` versions across the org" — combined with a join to `documents.organization_id`; a `(status)` index with a low-cardinality but highly-skewed distribution (few non-`READY` rows at any time) is effective here, optionally as a partial index `WHERE status != 'READY'`.

### Document Pages
- `(document_version_id, page_number)` UNIQUE — page-by-number lookup, the dominant access pattern (viewer navigation, citation resolution).

### Document Sections
- `(document_version_id, parent_section_id)` — TOC-tree rendering (fetch by version, then walk children).

### Document Chunks
- `(document_version_id)` — "all chunks for this version," used by ingestion/re-embedding jobs and by comparison.
- `(page_id)` — "all chunks on this page" (source-preview context expansion).
- `(section_id)` — "all chunks in this section" (comparison's section-level change grouping).
- `(organization_id)` — leading predicate on every retrieval query (see [§8](#8-multi-tenant-architecture)); in practice this is combined with the vector/FTS indexes below via planner-level bitmap-and, so it's kept as a plain B-tree rather than folded into the HNSW index (pgvector HNSW does not support composite/filtered indexing natively in the version targeted for V1).
- **HNSW** `USING hnsw (embedding vector_cosine_ops)` — semantic search, see [§17](#17-embedding-and-pgvector-design).
- **GIN** `USING GIN (content_tsv)` — keyword search, see [§18](#18-hybrid-search-architecture).

### Conversations / Messages / Citations
- `(organization_id, user_id, updated_at DESC)` on `conversations` — the conversation list, sorted by recency, scoped to the user.
- `(conversation_id, created_at)` on `messages` — fetch a conversation's transcript in order.
- `(message_id)` on `citations` — fetch all citations for a message (used on every assistant message render).
- `(chunk_id)` on `citations` — supports the (less frequent but valuable) reverse lookup "what questions has this passage ever been cited in," useful for content-owner insight into which sections get relied upon most.

### Processing Jobs
- `(document_version_id, status)` — "current job state for this version," polled/subscribed by the frontend's processing UI.
- `(status, created_at)` — worker/ops queries like "oldest pending jobs," "all failed jobs in the last 24h" (Dashboard's Failed Jobs widget).

### Audit Logs
- `(organization_id, created_at DESC)` — the Audit Log screen's default view.
- `(organization_id, resource_type, resource_id)` — "full history for this specific document/user."
- Consider **range partitioning by month** on `created_at` once volume warrants it (see [§36](#36-performance-considerations)) — indexes are defined per-partition, keeping each index small regardless of total historical volume.

### Comparisons / Conflicts
- `(document_a_version_id, document_b_version_id)` UNIQUE on `document_comparisons` — reuse/dedup, see [§25](#25-document-comparison-model).
- `(comparison_id, severity)` on `comparison_changes` — severity-filtered changes list.
- `(organization_id, status)` on `conflicts` — the Conflicts view's default "open conflicts" query.

---

## 28. Constraints and Referential Integrity

**Primary keys:** every table uses a `uuid` surrogate primary key (`gen_random_uuid()`, requiring the `pgcrypto` extension, or `uuid_generate_v4()` via `uuid-ossp` — either is acceptable; this document assumes `pgcrypto`'s `gen_random_uuid()` as it's built into modern PostgreSQL without an extra extension). UUIDs are chosen over serial integers because: (a) they're safe to generate client-side or in application code before insert, useful for the "create the row before uploading the file" pattern in [§35](#35-backup-and-recovery); (b) they don't leak sequential business volume (row-count-as-a-secret) across tenants; (c) they merge cleanly across any future sharding/replication topology. Exception: `audit_logs` may use a `bigint` identity column instead if/when partitioned, purely for partition-key/index efficiency at very high insert volume — a judgment call left to the implementation team based on observed volume, not a V1 requirement.

**Foreign keys:** every relationship in [§10](#10-entity-relationship-diagram) is a real, enforced foreign key, with one deliberate, documented exception (`audit_logs.resource_id`, [§24](#24-audit-log-model)).

**Cascading behavior — chosen per relationship's semantics, not defaulted blindly:**

| Relationship | On delete of parent | Reasoning |
|---|---|---|
| `document_versions.document_id → documents` | `CASCADE` | A version cannot exist without its document; deleting a document (hard delete, post-retention) removes its versions |
| `document_pages/sections/chunks.document_version_id → document_versions` | `CASCADE` | Structural children have no independent existence |
| `document_sections.parent_section_id → document_sections` | `CASCADE` | Deleting a section removes its subtree |
| `document_chunks.section_id → document_sections` | `SET NULL` | A chunk should not disappear if only its section metadata is removed/restructured; it degrades to "unsectioned" rather than vanishing |
| `citations.chunk_id/page_id/document_version_id → …` | `RESTRICT` (no cascade) | A citation is a historical record of what an AI answer relied on — the source chunk must not be deletable while a citation still references it; deletion of the owning document (post-retention) must go through the explicit deletion flow ([§33](#33-document-deletion-data-flow)) which handles citations deliberately rather than relying on cascade to silently corrupt conversation history |
| `messages.conversation_id → conversations` | `CASCADE` | Messages have no existence outside their conversation |
| `citations.message_id → messages` | `CASCADE` | Citations have no existence outside their message |
| `user_roles/role_permissions → users/roles/permissions` | `CASCADE` | Pure join-table rows, no independent meaning |
| `comparison_changes.old_chunk_id/new_chunk_id → document_chunks` | `SET NULL` | Preserve the comparison's denormalized text snapshot even if the underlying chunk is later removed |
| `conflicts.resolved_by`, `documents.owner_id`, etc. (`→ users`) | `RESTRICT`, paired with user soft-delete | A user should never be hard-deleted while still referenced as an owner/resolver/actor; see [§29](#29-soft-delete-strategy) |

**Unique constraints:** enumerated per-table throughout §§11–26; the recurring pattern is `(organization_id or parent_id, natural_key)` — uniqueness is almost always scoped *within* a tenant or parent, never global, reflecting the multi-tenant model.

**NOT NULL constraints:** applied to every column where the domain genuinely requires a value for the row to be meaningful (e.g., `documents.organization_id`, `documents.owner_id`, `citations.quoted_text`) — nullability is treated as a deliberate modeling decision throughout this document, not a default.

**Check constraints — representative examples:**

```sql
ALTER TABLE documents
  ADD CONSTRAINT documents_document_type_check
  CHECK (document_type IN ('policy','procedure','sop','contract','technical','regulatory','hr','marketing','other'));

ALTER TABLE document_versions
  ADD CONSTRAINT document_versions_status_check
  CHECK (status IN ('UPLOADED','PROCESSING','EXTRACTING','OCR','EMBEDDING','INDEXING','READY','FAILED'));

ALTER TABLE messages
  ADD CONSTRAINT messages_role_check
  CHECK (role IN ('USER','ASSISTANT','SYSTEM'));

ALTER TABLE comparison_changes
  ADD CONSTRAINT comparison_changes_type_check
  CHECK (change_type IN ('ADDED','REMOVED','MODIFIED'));

ALTER TABLE message_feedback
  ADD CONSTRAINT message_feedback_rating_check
  CHECK (rating IN (-1, 1));
```

**Tenant-isolation-specific constraints:** because `document_chunks.organization_id` is denormalized (deliberately, [§16](#16-chunking-and-rag-data-model)), a **trigger** (not just an application convention) enforces it can never drift from its parent document's actual organization:

```sql
CREATE OR REPLACE FUNCTION enforce_chunk_org_matches_document()
RETURNS trigger AS $$
BEGIN
  IF NEW.organization_id <> (
    SELECT d.organization_id
    FROM document_versions v JOIN documents d ON d.id = v.document_id
    WHERE v.id = NEW.document_version_id
  ) THEN
    RAISE EXCEPTION 'chunk organization_id does not match owning document organization_id';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_chunk_org_consistency
  BEFORE INSERT OR UPDATE ON document_chunks
  FOR EACH ROW EXECUTE FUNCTION enforce_chunk_org_matches_document();
```

This closes the one integrity gap a denormalized tenant column would otherwise introduce — it is not possible for the single most security-critical column in the schema to silently become wrong.

---

## 29. Soft Delete Strategy

**Entities using `deleted_at` (soft delete):** `documents`, `users`, `collections`, `conversations`.

**Why:** these are all entities where (a) other rows reference them and an abrupt hard delete would either cascade destructively or require awkward nullable-everywhere modeling, and (b) the product has a real "undo"/"restore," audit, or compliance need — a user might accidentally delete a document and need it back; a deactivated employee's document ownership history must remain intact for audit purposes even though they can no longer log in.

**Entities that do NOT use soft delete (their lifecycle is fully owned by a parent, or they're insert-only):** `document_versions`, `document_pages`, `document_sections`, `document_chunks` (deleted via real `DELETE`/cascade only when their owning `document`/`document_version` is hard-deleted, per [§33](#33-document-deletion-data-flow)); `messages`, `citations` (immutable conversation history — a conversation can be archived via its own `deleted_at`, but individual messages within it are not selectively deletable); `audit_logs` (insert-only, purged only by retention policy, never "soft" — see [§24](#24-audit-log-model)).

**When permanent (hard) deletion should occur:**
1. **User-initiated hard delete** is never immediate for `documents` — a soft-deleted document enters a **grace period** (e.g., 30 days, configurable per org in Settings) during which it can be restored, visible only in a "Trash"/"Recently deleted" view.
2. **After the grace period expires**, a scheduled background job performs the full hard-deletion cascade ([§33](#33-document-deletion-data-flow)) — removing chunks, pages, sections, versions, object-storage files, and finally the `documents` row itself.
3. **Users** are soft-deleted (deactivated) on offboarding but are **never hard-deleted** while any FK-referencing row exists that must retain provenance (document ownership history, audit log actor, citation trail) — practically, this means user hard-deletion is rare/manual and typically superseded by anonymization (replacing PII fields while retaining the row for referential integrity) if required by a data-subject-deletion request (e.g., GDPR), rather than a `DELETE`.
4. **Collections/Conversations** follow the same grace-period-then-purge pattern as documents, on a simpler timeline since they carry less regulatory weight.

**Excluding soft-deleted documents from RAG retrieval — enforced at multiple layers:**
- Every retrieval query joins through `documents` (directly or via `document_versions.document_id`) and includes `documents.deleted_at IS NULL` as a hard predicate, identical in spirit to the `organization_id` predicate — never optional, never a post-filter.
- The moment a document is soft-deleted, a synchronous (not queued) update immediately flips a `is_searchable` derived condition (computed as `deleted_at IS NULL AND status = 'READY'`, not a separately-stored flag that could drift) — because retrieval always re-checks `deleted_at` live rather than trusting a cached "is indexed" flag, there is no propagation delay where a just-deleted document remains answerable.
- The vector/FTS indexes themselves are not modified at soft-delete time (that would be wasteful for something that might be restored within the grace period) — exclusion is purely a query-time `WHERE` predicate, cheap given `deleted_at` participates in the partial indexes described in [§27](#27-indexing-strategy).

---

## 30. Data Lifecycle

**Ingestion:**

```text
Upload
 ↓  (sync)      Object Storage           — file bytes written, storage_key returned
 ↓  (sync)      Document + Version record — status = UPLOADED, storage_key persisted
 ↓  (async)     Processing Job created    — status = PENDING, enqueued to Redis
 ↓  (async)     Text Extraction            — document_versions.status = EXTRACTING
 ↓  (async)     OCR if required             — status = OCR (skipped if not a scanned doc)
 ↓  (async)     Pages persisted              — document_pages rows written
 ↓  (async)     Sections detected              — document_sections rows written
 ↓  (async)     Chunks created                  — document_chunks rows written, status = EMBEDDING
 ↓  (async)     Embeddings generated              — document_chunks.embedding populated
 ↓  (async)     Search index finalized              — status = INDEXING → READY
```

**Deletion:**

```text
Delete Request
 ↓  (sync)      documents.deleted_at set                — immediately excluded from retrieval (§29)
 ↓  (async, after grace period)  Remove from Retrieval    — already excluded live; this step is a no-op confirmation
 ↓  (async)     Delete embeddings/chunks                  — document_chunks rows removed
 ↓  (async)     Delete pages/sections                       — document_pages/document_sections rows removed
 ↓  (async)     Delete document_versions rows                — cascades from above
 ↓  (async)     Delete Object Storage files                    — original files + any rendered assets purged
 ↓  (sync, final step) Delete documents row (or retain a tombstone if required by audit policy)
```

**Synchronous vs. asynchronous — explicit rule:** anything that affects **user-visible correctness or security** (a document becoming unsearchable, a file existing before its DB row claims it does) is synchronous and transactional. Anything that is **pure processing work** (OCR, chunking, embedding, and the multi-step teardown of a deleted document's derived data) is asynchronous, tracked via `processing_jobs`, and safely retryable/idempotent — because none of it is on the critical path of "is this action safe/correct right now," only "is this action complete yet," which the frontend already models as a first-class processing state ([§11](#11-real-time-processing-ux) of the companion Frontend Design Documentation).

---

## 31. RAG Query Data Flow

**Scenario:** user asks *"What is the approval process?"*, scoped to two selected documents (Marketing Policy 2026, Approval SOP).

```text
User Question
      ↓
messages (INSERT — role=USER, PostgreSQL)
      ↓
Embedding Model (application/inference service — not a storage step)
      ↓
pgvector similarity search (PostgreSQL, document_chunks.embedding)
      ↓
document_chunks (candidate rows: content, page_id, section_id)
      ↓
Metadata Filtering (PostgreSQL WHERE: organization_id, document_id IN (...), current-version scoping)
      ↓
Hybrid Search merge (RRF over vector + tsvector results, computed in SQL/application layer)
      ↓
Reranking (application/inference service, operates on the merged candidate set — not a storage step)
      ↓
LLM generation (application/inference service, given top-K reranked chunks as context)
      ↓
citations (INSERT — one row per source chunk actually used in the answer, PostgreSQL)
      ↓
messages (INSERT — role=ASSISTANT, content + model + token/latency/groundedness fields, PostgreSQL)
```

**Which component participates at each step:**

| Step | Storage/system involved |
|---|---|
| Persist the question | PostgreSQL (`messages`) |
| Embed the question | Inference service (no storage write) |
| Vector search | PostgreSQL / pgvector (`document_chunks.embedding`, HNSW index) |
| Keyword search | PostgreSQL (`document_chunks.content_tsv`, GIN index) |
| Metadata filter | PostgreSQL (`documents`, `document_versions`, `conversation_documents`) |
| Merge/rerank | Application layer, reading from the two PostgreSQL result sets above (Redis may cache the merged candidate set briefly if the same query pattern repeats, but this is an optimization, not a required step) |
| Generate answer | Inference service (LLM), fed the reranked chunk `content` values |
| Persist citations | PostgreSQL (`citations`, one row per chunk actually cited, with `char_start`/`char_end` extracted by matching the LLM's claim against the source chunk) |
| Persist the answer | PostgreSQL (`messages`) |
| Stream to frontend | Application layer via SSE (not a storage step; see companion Frontend spec §11) |

**Full 10-step scenario walkthrough** (organization has: Marketing Policy 2025, Marketing Policy 2026, Approval SOP, Regulatory Guidelines):

1. **Identify the organization** — resolved from the authenticated session (JWT/session claim), never from client input; becomes the mandatory `organization_id` filter on every subsequent query.
2. **Determine accessible documents** — `documents WHERE organization_id = :org AND deleted_at IS NULL AND (access_level = 'organization' OR EXISTS (permission grant))`, intersected with the conversation's explicit scope from `conversation_documents` (Marketing Policy 2026 + Approval SOP only, per this scenario — Marketing Policy 2025 and Regulatory Guidelines are excluded by scope even though accessible).
3. **Identify the latest effective version** — for each in-scope document, `documents.current_version_id` resolves directly to the version whose chunks should be searched (Marketing Policy 2026's current version, not 2025 — the user's phrasing "current approval process" maps to current-version scoping rather than an explicit `effective_date` override).
4. **Retrieve relevant chunks** — pgvector ANN search over `document_chunks` filtered to the two resolved version IDs.
5. **Perform hybrid search** — the same filtered set additionally scored via `content_tsv` keyword match; both result sets merged via RRF.
6. **Rerank** — top candidates (e.g., top 20 by RRF) passed to a cross-encoder rerank step, trimmed to the top 5–8 actually sent to the LLM.
7. **Generate the answer** — LLM produces the answer text with inline reference markers tied to specific source chunks.
8. **Create citations** — one `citations` row per chunk the LLM actually drew on, each carrying `document_id`, `document_version_id`, `page_number`, `section`, and the exact `quoted_text`.
9. **Store the conversation** — the `USER` and `ASSISTANT` messages are both persisted to `messages`, `conversations.updated_at` bumped for recency ordering.
10. **Allow the frontend to open the exact source page** — the frontend needs nothing beyond what's already in `citations`: `document_version_id` + `page_number` resolves the exact file/page to render, and `quoted_text`/`char_start`/`char_end` drives the highlight overlay — no additional query round-trip is required beyond what was already returned with the message.

---

## 32. Document Ingestion Data Flow

```text
1. Client requests upload → backend generates document_id + document_version_id (application-generated UUIDs, before any storage write)
2. backend computes storage_key deterministically (e.g., org_id/document_id/version_id/original.ext)
3. document_versions row INSERTed — status = 'UPLOADED', storage_key already set (see §35 for why this ordering matters)
4. File uploaded to Object Storage (direct client→storage via signed URL, or proxied through the backend)
5. Upload-confirmation callback flips status if needed (only relevant if using signed-URL direct upload, to confirm completion)
6. processing_jobs row INSERTed (job_type = EXTRACTION, status = PENDING) + task enqueued to Redis
7. Worker claims the job (Redis) → processing_jobs.status = PROCESSING, started_at set
8. Worker performs extraction → document_pages rows written → document_versions.status = EXTRACTING → (if scanned) OCR → status = OCR
9. Worker detects structure → document_sections rows written
10. Worker chunks content → document_chunks rows written (embedding = NULL initially) → status = EMBEDDING
11. Worker calls embedding model, batches updates to document_chunks.embedding → status = INDEXING
12. Worker finalizes (HNSW/GIN indexes update incrementally, no explicit step needed) → document_versions.status = READY
13. documents.current_version_id updated (if this version's effective_date makes it the new current version)
14. processing_jobs.status = COMPLETED, completed_at set
15. audit_logs row INSERTed (action = DOCUMENT_UPLOADED)
```

Steps 1–3 are synchronous (part of the initial HTTP request/response); steps 6 onward are asynchronous, independently retryable per stage (each stage can be its own `processing_jobs` row, `job_type` distinguishing `EXTRACTION`/`OCR`/`CHUNKING`/`EMBEDDING`/`INDEXING`, so a failure partway through does not require re-running already-completed stages).

---

## 33. Document Deletion Data Flow

```text
1. User requests deletion → authorization check (role/permission)
2. documents.deleted_at = now() (synchronous, transactional)          ⟶ immediately excluded from retrieval (§29)
3. audit_logs row INSERTed (action = DOCUMENT_DELETED)                 (synchronous)
4. Response returned to user — deletion "complete" from their perspective
──────────────────────────────────────────────────────────────────────
   [Grace period — e.g. 30 days — document remains restorable]
──────────────────────────────────────────────────────────────────────
5. Retention-purge job (scheduled) selects documents past grace period
6. processing_jobs row INSERTed (job_type = PURGE)
7. Worker deletes document_chunks rows (and thus their embeddings) for all versions
8. Worker deletes document_pages, document_sections rows for all versions
9. Worker deletes document_versions rows
10. Worker deletes Object Storage files (original + rendered assets) for every version's storage_key
11. Worker hard-deletes (or anonymizes/tombstones, per org audit policy) the documents row
12. audit_logs row INSERTed (action = DOCUMENT_PURGED)
```

**Note on citations:** because `citations → document_chunks/pages/versions` uses `RESTRICT` ([§28](#28-constraints-and-referential-integrity)), step 7–9 above cannot silently orphan historical AI answers — the purge worker must explicitly handle existing citations pointing at the document being purged, typically by nulling the FK columns while preserving `citations.quoted_text` (already stored as plain text, independent of the live chunk) so historical conversations remain readable ("this answer cited a document that has since been deleted") rather than either blocking the purge indefinitely or corrupting conversation history.

---

## 34. Security and Tenant Isolation

- **Tenant isolation:** layered as described in [§8](#8-multi-tenant-architecture) — FK constraints, mandatory application-layer scoping, recommended RLS, and a trigger-enforced invariant on the one denormalized tenant column (`document_chunks.organization_id`, [§28](#28-constraints-and-referential-integrity)).
- **Database credentials:** the application connects with a role that has no `SUPERUSER`/`CREATEDB` privileges and no `DDL` grants at runtime (migrations run under a separate, more privileged role, out of the application's request path); `UPDATE`/`DELETE` explicitly revoked on `audit_logs` ([§24](#24-audit-log-model)).
- **Encryption at rest:** managed via the cloud provider's disk/volume encryption for PostgreSQL and Redis persistence, and server-side encryption (SSE) on the object storage bucket; `password_hash` uses a strong adaptive hash (bcrypt/argon2) — plaintext passwords are never persisted anywhere, including logs.
- **Encryption in transit:** TLS enforced for all client↔API, API↔PostgreSQL, API↔Redis, and API↔object-storage connections; `sslmode=require` (or stricter) on the PostgreSQL connection string.
- **Secrets management:** database credentials, object-storage keys, and embedding/LLM API keys are sourced from a secrets manager/vault (cloud-native KMS or equivalent) injected as runtime environment/secret-mount values — never committed to source control or stored in plain config tables.
- **Storage access:** the object storage bucket is **not publicly readable**; all client access to file bytes goes through short-lived, backend-issued **signed URLs**, generated only after the backend has verified the requesting user's `organization_id` and `access_level` permissions against the `documents`/`document_versions` rows.
- **RBAC:** enforced via the `roles`/`permissions`/`user_roles`/`role_permissions` schema ([§11](#11-identity-schema)); every API endpoint declares its required permission key, checked against the authenticated user's resolved permission set before any query executes.
- **Row-level security:** recommended defense-in-depth, detailed in [§8](#8-multi-tenant-architecture).
- **Audit logs:** immutable, insert-only, covering the security-relevant action catalog in [§24](#24-audit-log-model).
- **Data deletion:** governed by the soft-delete-then-purge lifecycle ([§29](#29-soft-delete-strategy), [§33](#33-document-deletion-data-flow)), which is itself a security control — it ensures deletion requests (including regulatory right-to-erasure requests) have a bounded, auditable completion path rather than being "best effort."
- **Backup security:** see [§35](#35-backup-and-recovery) — backups are encrypted at rest and access to restore operations is restricted to a small operational role set, since a database backup is itself a full copy of every tenant's sensitive documents' metadata and every chunk's text content.

---

## 35. Backup and Recovery

**PostgreSQL:**
- **Automated daily full snapshots** plus **continuous WAL archiving**, enabling **point-in-time recovery (PITR)** to within minutes of any point in the retention window (typically 30 days, tunable per compliance requirements).
- Backups are encrypted at rest and stored redundantly (ideally cross-region) from the primary database's region.
- Restore drills (periodic, not just "backups exist") are an operational requirement, not a schema concern, but are flagged here since an untested backup is not a real recovery capability.

**Object Storage:**
- **Bucket versioning enabled** — protects against accidental overwrite/delete of an original file independent of the PostgreSQL backup cycle.
- **Lifecycle policies** move older/rarely-accessed version files to cooler storage tiers without affecting availability, purely a cost optimization, not a durability concern (durability is provided by the storage provider's native replication).

**Redis:**
- **No durable backup is required by architecture design** — every piece of Redis state is either reconstructable from PostgreSQL (`processing_jobs` rows re-enqueue naturally on worker restart via a reconciliation sweep) or safely disposable (caches, rate-limit counters). Optional AOF/RDB persistence may be enabled purely to reduce a full cache-cold-start after a Redis restart, as a performance nicety, never as a durability requirement.

**Disaster recovery:** the DR posture is defined by PostgreSQL's cross-region replica/backup strategy and object storage's cross-region replication being kept in sync in *time*, not just individually recoverable — a restore must bring both stores back to a consistent point, which is achievable because every `document_versions.storage_key` is immutable and content-addressed-in-spirit (never overwritten in place; a re-upload creates a new version with a new key), so restoring PostgreSQL to timestamp T and object storage to timestamp T (or later, since old keys are never deleted except via the deliberate purge flow) always yields a consistent, dereferenceable state.

**Consistency between PostgreSQL and Object Storage — the two failure modes explicitly addressed:**

**Database succeeds, but file upload fails:**
- Prevented from being a real problem by *ordering*: the `document_versions` row is created with `status = 'UPLOADED'` and its `storage_key` **before** the upload is confirmed complete (see [§32](#32-document-ingestion-data-flow), step 3 precedes step 4). If the upload subsequently fails, the version simply never progresses past `UPLOADED`/never receives a successful upload-confirmation callback — the frontend already models this as a distinct, visible processing state (not a silent gap), and a reconciliation job can detect and flag/clean up versions stuck in `UPLOADED` beyond a timeout.

**File upload succeeds, but the database transaction fails:**
- Because the `document_versions` row is created *before* the upload begins (not after), this specific ordering means "upload succeeds but DB insert fails" **cannot occur** as described — the DB row always exists first. The remaining edge case is a *later* DB write failing (e.g., a status-update after upload confirmation) — in that case, an orphaned file may exist at `storage_key` referenced by a row stuck in `UPLOADED`. A periodic **orphan-reconciliation job** compares object-storage keys under each organization's prefix against known `storage_key` values in `document_versions` and flags/cleans up unreferenced objects older than a safety window (e.g., 24 hours, to avoid racing an upload still in progress).
- This "create the record first, then write bytes, then confirm" pattern is a deliberate **saga-style state machine** substituting for a two-phase distributed transaction (which PostgreSQL and an object store cannot natively share) — it trades a small window of "recorded but not yet confirmed" state (always visible and recoverable) for the strong guarantee that a PostgreSQL row referencing a `storage_key` is never created *after* the fact from an upload the system didn't already know about.

---

## 36. Performance Considerations

**Volume math (why pgvector is sufficient at V1, concretely):** a mid-size enterprise customer with 10,000 documents, averaging 30 pages and ~8 chunks/page, yields ~2.4M chunks. A large customer at 100,000 documents yields ~24M chunks. HNSW in pgvector comfortably serves ANN queries in this range with sub-100ms latency on appropriately-sized hardware (adequate `shared_buffers`/RAM to hold the index working set) — well within the tens-of-millions ceiling referenced in [§5](#5-why-pgvector). Multi-tenant deployments spread this volume across many `organization_id`-filtered subsets, which in practice narrows the effective candidate set per query well below the corpus-wide total.

**Indexing:** covered exhaustively in [§27](#27-indexing-strategy) — the guiding principle applied throughout is "index the leading predicate of the platform's actual hot queries," not "index every column."

**Pagination:** all list endpoints (`documents`, `search`, `audit_logs`) use **keyset (cursor-based) pagination** on `(created_at, id)` rather than `OFFSET`-based pagination once tables grow large — `OFFSET` degrades linearly with page depth (PostgreSQL must still scan/discard all skipped rows), while keyset pagination stays O(log n) via the supporting index regardless of how deep the user pages. `OFFSET`-based pagination is acceptable only for small, bounded result sets (e.g., paginating a single document's ~40 pages).

**Partitioning:** not required at V1 launch, but the schema is designed to make it a non-disruptive later addition:
- **`audit_logs`** is the most likely first partitioning candidate — range-partitioned by month on `created_at` once monthly volume materially exceeds comfortable single-table vacuum/index-maintenance windows (a judgment call made from observed metrics, not a fixed row-count threshold).
- **`document_chunks`** could be partitioned by `organization_id` hash (or a tenant-tier boundary) at a scale where a single large enterprise tenant's chunk volume starts to dominate query planning for smaller tenants sharing the same table/index — not anticipated at V1 volumes given the math above.

**Query optimization:** every new query pattern introduced during implementation should be validated with `EXPLAIN (ANALYZE, BUFFERS)` against representative data volume before merging, specifically checking that the planner is using the intended indexes (especially the HNSW/GIN indexes, which the planner will sometimes bypass in favor of a sequential scan on small/test-sized tables — expected and fine at low row counts, but must be re-verified at realistic volume).

**Connection pooling:** required from day one, not a later optimization — PostgreSQL's per-connection memory overhead makes unpooled connections from an autoscaling API tier a fast path to connection exhaustion. **PgBouncer** (transaction-pooling mode) sitting between the FastAPI application and PostgreSQL is the recommended V1 approach; note that transaction-pooling mode has implications for session-scoped features like `SET LOCAL app.current_org_id` (used for RLS, [§8](#8-multi-tenant-architecture)) — it must be set within the same transaction as the queries relying on it, which transaction-pooling mode supports correctly as long as it's not set outside a transaction block and assumed to persist across pooled connections.

**Vacuum/analyze:** `document_chunks` (frequent inserts, occasional bulk re-embedding updates) and `audit_logs` (high insert volume, no updates) both warrant **tuned autovacuum settings** (more aggressive scale factors than PostgreSQL defaults) rather than relying on default autovacuum thresholds, to keep the HNSW/GIN indexes and table statistics fresh under sustained write load — a standard PostgreSQL operational practice, called out here because both tables are directly on the platform's hottest paths (ingestion and every request, respectively).

**Redis caching:** search-result and hot-document-metadata caching (short TTL, e.g., 30–120s) reduces repeated load on PostgreSQL for popular queries/documents without introducing staleness the product can't tolerate (search results are inherently "as of a moment," and document metadata changes infrequently relative to read volume).

**Background processing:** ingestion stages ([§32](#32-document-ingestion-data-flow)) are deliberately decomposed into independently-retryable jobs rather than one monolithic pipeline task, so a transient embedding-provider outage, for example, doesn't force re-running (and re-billing) OCR/extraction that already succeeded.

**Large file handling:** uploads stream directly to object storage (client-to-storage via signed URL where feasible, or streamed through the backend without full in-memory buffering otherwise) — the application and database are never in the business of holding a 100MB file in memory or in a database row.

---

## 37. Scalability Strategy

### V1

```text
PostgreSQL + pgvector
+
Redis
+
Object Storage (Azure Blob / S3)
```

Sufficient for the volume math in [§36](#36-performance-considerations), operationally simplest, and keeps the platform's entire retrieval consistency story inside a single database.

### Larger scale — when justified, not preemptively

```text
PostgreSQL + pgvector   (remains system of record for all relational data)
+
Redis
+
Object Storage
+
OpenSearch / Elasticsearch   (dedicated search/retrieval layer, if/when justified)
```

**A dedicated search engine or vector database becomes justified when one or more of these becomes concretely true (not hypothetically true):**
- Chunk volume grows past the range where HNSW query latency at acceptable `ef_search` settings no longer meets the product's latency target on reasonably-provisioned hardware (i.e., the [§36](#36-performance-considerations) math is empirically exceeded, not merely approached).
- The product needs **search features PostgreSQL's FTS genuinely can't do well** — e.g., sophisticated relevance tuning, faceted aggregation at scale, fuzzy/typo-tolerant search across huge corpora, or multi-language analyzers beyond what `tsvector` configurations reasonably support.
- The platform needs **independent horizontal scaling of the retrieval workload** away from the transactional/relational workload — e.g., search query volume growing much faster than write/transactional volume, to the point where they contend for the same PostgreSQL resources in a way connection pooling and read replicas can no longer absorb.
- Multi-region **active-active** retrieval becomes a requirement (PostgreSQL's replication model is primarily single-writer; a dedicated distributed search engine may fit a true multi-region-write requirement better).

**In this scenario, PostgreSQL does not stop being the system of record** — `document_chunks` and its relational metadata remain authoritative in PostgreSQL; a search engine would be fed via change-data-capture or a dual-write/outbox pattern from PostgreSQL, becoming a derived, rebuildable index rather than a second source of truth — preserving Principle 10 (see [§42](#42-implementation-recommendations)/[§8](#8-multi-tenant-architecture) philosophy) even at larger scale.

**Explicitly not introduced preemptively:** a dedicated vector database, a graph database, a document store (Mongo-style), or a separate OLAP warehouse for analytics — none of these are justified by anything in this specification's V1 requirements, and each would add a consistency boundary the platform does not currently need.

---

## 38. Observability

| Metric | Source |
|---|---|
| Query latency (general) | PostgreSQL `pg_stat_statements` |
| Vector search latency | Application-layer timing around the pgvector query, logged/exported (PostgreSQL doesn't natively separate this from general query latency) |
| Database connections | PostgreSQL `pg_stat_activity` / PgBouncer stats |
| Storage usage | Object storage provider metrics (bucket size, request counts); PostgreSQL `pg_database_size`/table sizes |
| Chunk count / embedding count | Periodic `SELECT count(*)` (or `pg_stat_user_tables.n_live_tup` estimate for cheap approximate monitoring) surfaced to the Analytics domain |
| Processing jobs (throughput, failure rate) | PostgreSQL `processing_jobs` aggregate queries, feeding the Analytics "Processing Jobs" widget directly |
| Failed jobs | `processing_jobs WHERE status = 'FAILED'`, also driving the Dashboard's Failed Jobs widget |
| RAG retrieval metrics (retrieval latency, citation coverage, grounded-answer %) | Computed from `messages`/`citations` columns (`retrieval_ms`, `groundedness`), aggregated by the Analytics domain |
| LLM usage / token usage | `messages.prompt_tokens`/`completion_tokens`, aggregated per day/model for the Analytics cost view |
| Redis health (queue depth, cache hit ratio) | Redis `INFO` command / provider-native metrics |
| Infrastructure-level (CPU, memory, disk I/O, replication lag) | External observability platform (cloud-native monitoring, or Prometheus/Grafana/Datadog), not PostgreSQL/Redis themselves |

**Placement principle:** PostgreSQL and Redis natively expose **operational** metrics about themselves (connections, query stats, memory); **business/product** metrics (grounded-answer %, citation coverage, token cost) are **computed from the data already stored** (`messages`, `citations`, `processing_jobs`) rather than requiring a separate metrics pipeline — this is a direct benefit of putting these facts in relational tables rather than, say, only logging them to an external APM tool where they'd be harder to join against the rest of the domain model for product analytics (e.g., "grounded-answer % broken down by document type" is a straightforward SQL join in this schema).

---

## 39. Recommended V1 Architecture

```text
┌─────────────────────────────────────────────┐
│                React Frontend               │
└──────────────────────┬──────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────┐
│                   FastAPI                    │
└───────────────┬───────────────┬──────────────┘
                │               │
                ▼               ▼
       ┌────────────────┐   ┌──────────────┐
       │ PostgreSQL      │   │ Redis        │
       │                 │   │              │
       │ Relational      │   │ Job Queue    │
       │ Data            │   │ Cache        │
       │                 │   │ Rate Limit   │
       │ pgvector        │   │              │
       │ Embeddings      │   │              │
       └───────┬─────────┘   └──────┬───────┘
               │                    │
               │                    ▼
               │                 Workers
               │                    │
               │             ┌──────┴──────┐
               │             │ OCR         │
               │             │ Extraction  │
               │             │ Chunking    │
               │             │ Embedding   │
               │             └─────────────┘
               │
               ▼
       ┌─────────────────┐
       │ Azure Blob / S3 │
       │                 │
       │ Original Files  │
       └─────────────────┘
```

**Why this is preferred for V1, restated as a single decisive argument:** every piece of this architecture either *is* the system of record (PostgreSQL), *serves* the system of record without duplicating its truth (Redis, Object Storage), or *derives from* the system of record on demand (worker output, ultimately written back to PostgreSQL). There is exactly one place — PostgreSQL — where "what does the platform know" is answered, which is precisely the property that makes the explainability principle ([§42](#42-implementation-recommendations)) enforceable: every citation, every conflict, every comparison result is traceable through a single, consistent, transactional store, not reconciled across independently-evolving systems.

---

## 40. Future Architecture Evolution

Noted so V1 decisions don't foreclose them, mirroring the companion Frontend document's "Future UX Enhancements":

1. **Dedicated search engine** ([§37](#37-scalability-strategy)) — introduced as a derived index, not a second source of truth.
2. **`chunk_embeddings` extension table** ([§16](#16-chunking-and-rag-data-model)) — for multi-model/multi-version embedding support.
3. **Shared/team conversations** — `conversations.user_id` would need to become a many-to-many `conversation_members` relationship if collaborative research sessions (matching the frontend's "saved research sessions" future enhancement) are built.
4. **Row-Level Security promoted from recommended to required**, alongside a formal per-tenant encryption-key strategy if customer contracts demand tenant-specific encryption keys (envelope encryption per organization) rather than a shared database-level encryption-at-rest key.
5. **Read replicas** for PostgreSQL, splitting Analytics/reporting-heavy read traffic away from the transactional/RAG-serving primary, once Analytics query volume materially competes with request-serving latency.
6. **Custom RBAC beyond the three system roles** — the `roles`/`permissions` schema ([§11](#11-identity-schema)) already supports arbitrary org-defined roles; this is additive, not a redesign.
7. **Multi-language document support** — would extend `document_chunks.content_tsv` to a per-language `tsvector` configuration and may require per-language embedding models, tracked via the existing `embedding_model` column.
8. **Data warehouse / OLAP layer** for deep historical analytics, fed from PostgreSQL via CDC/ETL, once Analytics needs (e.g., long-window trend analysis across millions of historical messages) outgrow what's comfortable to compute against the live transactional schema directly.

---

## 41. Architectural Decisions and Trade-offs

| Decision | Alternative(s) rejected | Why |
|---|---|---|
| PostgreSQL + pgvector for both relational and vector data | Separate vector DB (Pinecone/Weaviate/Qdrant) | Single consistency domain, combined metadata+vector filtering, sufficient V1 performance, lower operational overhead ([§5](#5-why-pgvector)) |
| Redis for queue/cache only, never business data | PostgreSQL-as-queue (`SKIP LOCKED`) | Avoids polling load/lock contention on the primary transactional database ([§6](#6-why-redis)) |
| Object storage for all binaries | `bytea`/large objects in PostgreSQL | Cost, performance isolation, native versioning/lifecycle/signed-URL features ([§7](#7-why-object-storage)) |
| Shared DB, shared schema, row-level tenancy (+ recommended RLS) | Database-per-tenant, schema-per-tenant | Operationally simplest at this scale; RLS closes the residual risk without the migration/ops overhead of per-tenant provisioning ([§8](#8-multi-tenant-architecture)) |
| Documents and document_versions as separate tables | A single "documents" table with a version column, no version history | Versioning is core to the product (comparison, conflict detection, effective-date-aware RAG) and cannot be bolted on later without a painful migration ([§14](#14-document-versioning)) |
| Self-referencing `document_sections.parent_section_id` | Fixed-depth section/subsection columns | Real business documents nest to arbitrary, unpredictable depth ([§15](#15-page-and-section-model)) |
| Embedding column directly on `document_chunks` | Separate `chunk_embeddings` table from day one | 1:1 relationship at V1 (single production embedding model); avoids unnecessary join overhead on the hottest read path, with a documented, low-friction upgrade path when multi-model support is actually needed ([§16](#16-chunking-and-rag-data-model)) |
| HNSW index over IVFFlat | IVFFlat | Better fit for an incrementally, continuously ingested corpus; no manual re-tuning as data grows ([§17](#17-embedding-and-pgvector-design)) |
| RRF-based hybrid search (computed in SQL/app layer) | A single ranking signal (vector-only or keyword-only) | Neither signal alone is reliably best across all query types (short keyword queries vs. natural-language questions); RRF is a simple, well-understood, database-computable fusion method requiring no extra infrastructure ([§18](#18-hybrid-search-architecture)) |
| Heavily denormalized `citations` table | Fully normalized, joined at read time | Citations are on the platform's hottest, most latency-sensitive render path; normalized FKs retained for integrity, denormalized columns added purely for read performance ([§22](#22-citation-model)) |
| Comparisons persisted (not cache-only, not regenerate-always) | Cache-only or always-regenerate | Deterministic, audit-relevant, and directly linkable results; avoids both wasted recomputation and silent regeneration drift ([§25](#25-document-comparison-model)) |
| Conflicts persisted with an explicit review workflow | Dynamically generated on every view | The feature's own UX (mark-as-reviewed) requires durable state; dynamic generation would resurface dismissed conflicts indefinitely ([§26](#26-conflict-detection-model)) |
| `audit_logs.resource_id` not a foreign key | Foreign key with `SET NULL`/`CASCADE` | Preserves truthful historical record after the referenced resource is purged, without corrupting or cascading away audit history ([§24](#24-audit-log-model)) |
| Soft delete + grace period + scheduled hard purge | Immediate hard delete | Supports undo, matches compliance/audit expectations, and gives retrieval-exclusion an immediate (not eventually-consistent) guarantee while physical cleanup happens safely asynchronously ([§29](#29-soft-delete-strategy), [§33](#33-document-deletion-data-flow)) |
| "Create DB row before uploading bytes" ordering | Upload first, then write DB row | Eliminates the "file exists but nothing references it as canonical" failure mode by construction; the remaining edge cases are handled by a reconciliation job rather than a distributed transaction ([§35](#35-backup-and-recovery)) |

---

## 42. Implementation Recommendations

**Sequencing for implementation** (suggested build order, respecting dependencies):
1. Identity + Organizations ([§11](#11-identity-schema), [§12](#12-organization-schema)) — everything else depends on tenancy and auth existing first.
2. Documents + Document Versions ([§13](#13-document-schema), [§14](#14-document-versioning)) + Object Storage integration — establishes the ingestion entry point.
3. Processing Jobs ([§23](#23-processing-job-model)) + Redis queue wiring — needed before any real content pipeline can run.
4. Pages, Sections, Chunks ([§15](#15-page-and-section-model), [§16](#16-chunking-and-rag-data-model)) — the structural/retrieval backbone.
5. Embeddings + pgvector indexing ([§17](#17-embedding-and-pgvector-design)) — pin the production embedding model **before** this migration runs.
6. Hybrid search ([§18](#18-hybrid-search-architecture)) — validate with `EXPLAIN ANALYZE` against realistic seeded volume, not just empty/tiny tables.
7. Collections ([§19](#19-collection-model)) — low-risk, can parallelize with steps 4–6.
8. Conversations, Messages, Citations ([§20](#20-conversation-model)–[§22](#22-citation-model)) — the primary AI interaction path; citations schema should be finalized before frontend citation UI integration begins.
9. Comparisons + Conflicts ([§25](#25-document-comparison-model), [§26](#26-conflict-detection-model)) — depend on stable chunk/version data.
10. Audit Logs ([§24](#24-audit-log-model)) — should be wired incrementally alongside every domain above (each domain's write paths should emit their own audit events as they're built, not bolted on at the end).

**Governing principles restated (apply these when any implementation ambiguity arises that this document doesn't explicitly resolve):**

1. PostgreSQL is the **system of record**.
2. pgvector is the **semantic retrieval layer inside PostgreSQL**, not a separate system.
3. Object storage is responsible for **binary documents** only — never business facts.
4. Redis is responsible for **temporary/fast infrastructure concerns**, never the only copy of a business fact.
5. Every tenant-owned resource is isolated by `organization_id`, enforced at multiple layers, not just application convention.
6. Documents and document versions are **separate concepts** — never collapse them.
7. Pages, sections, and chunks **retain document structure and provenance** — never flatten content to lose its origin.
8. Every AI answer is **traceable to source chunks and pages** via the `citations` table — this is non-negotiable and should block any feature that can't satisfy it.
9. RAG retrieval **must respect authorization and tenant boundaries** in every query, with no code path that queries `document_chunks` without an `organization_id` predicate.
10. **Do not introduce additional storage technologies** beyond PostgreSQL/Redis/Object Storage unless a concrete, measured limitation (not a hypothetical one) justifies it — see [§37](#37-scalability-strategy) for the specific conditions that would justify each future addition.

---

*End of document.*
