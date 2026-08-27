# Phase 3 — Document Management Implementation Plan

## Goal

Implement the full document lifecycle: upload to object storage (S3/MinIO), metadata management, versioning, listing with filters/pagination, download via signed URLs, soft delete/restore, bulk operations, collections, and all required audit events — with complete tenant isolation and permission enforcement.

## Background

Phases 0–2 are complete: the project scaffold, database foundation, auth/RBAC/multi-tenancy, the `ObjectStorageProvider` abstraction (stub), and audit logging are all in place. Phase 3 plugs in the concrete S3/MinIO storage implementation, adds the documents/versions/tags/collections schema, and builds the full document service + API layer.

Phase 3 deliberately does **not** include processing pipelines (Phase 4), text extraction (Phase 5), or anything AI-related — it ends at `status=UPLOADED` with `202 Accepted`.

---

## Proposed Changes

### 1. Infrastructure — Real S3/MinIO Storage Provider

#### [MODIFY] [`storage.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/infrastructure/storage.py)

Replace the stub with a concrete `S3StorageProvider` (compatible with MinIO locally via the `boto3` SDK already in `requirements.txt`). Methods: `upload` (streaming), `download`, `generate_signed_url`, `delete`, `exists`. Wire into app lifespan startup in `main.py`.

---

### 2. Database — Migrations

#### [NEW] `alembic/versions/004_documents_versions_tags_collections.py`

New migration (depends on 003) creating:
- **`documents`** — logical document table (`organization_id`, `owner_id`, `name`, `document_type` CHECK, `status`, `access_level` CHECK, `current_version_id` nullable FK, `department`, `deleted_at`, timestamps)
- **`document_versions`** — version table (`document_id`, `version_number`, `version_label`, `effective_date`, `expiration_date`, `storage_key`, `mime_type`, `file_size_bytes`, `status` CHECK with UPLOADED/PROCESSING/EXTRACTING/OCR/CHUNKING/EMBEDDING/INDEXING/READY/FAILED, `error_message`, `page_count`, `created_by`)
- **`document_tags`** — N:M join (`document_id`, `tag`) — PK on both
- **`collections`** — (`organization_id`, `name`, `description`, timestamps) — UNIQUE `(organization_id, name)`
- **`collection_documents`** — N:M join (`collection_id`, `document_id`, `added_at`) — PK on both
- All indexes from DB §27 (partial `WHERE deleted_at IS NULL`, `organization_id` leading composites, `document_type`, `status`, `owner_id`, `created_at`)
- UNIQUE `(document_id, version_number)`

---

### 3. Models — SQLAlchemy ORM

#### [NEW] [`app/models/document.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/models/document.py)

SQLAlchemy models: `Document`, `DocumentVersion`, `DocumentTag`, `Collection`, `CollectionDocument`, with full relationships, typed mapped columns matching the migration, and appropriate `__table_args__`.

---

### 4. Domain — Business Rules

#### [NEW] [`app/domain/documents.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/domain/documents.py)

Pure, unit-testable domain functions:
- `ALLOWED_MIME_TYPES` / `ALLOWED_EXTENSIONS` allow-list
- `validate_file_type(filename, content_type, magic_bytes)` — magic-byte sniff + extension + MIME check
- `MAX_FILE_SIZE_BYTES` constant (org-configurable in future)
- `compute_storage_key(org_id, doc_id, version_id, extension)` — deterministic `organizations/{org_id}/documents/{doc_id}/versions/{version_id}/original.{ext}`
- `DocumentType` / `AccessLevel` / `VersionStatus` enums
- `DocumentStatus` enum for logical document (`active`/`archived`)

---

### 5. Repositories

#### [NEW] [`app/repositories/document_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/document_repository.py)

`DocumentRepository(TenantScopedRepository[Document])` with:
- `create(...)`, `get_by_id_for_org(id, org_id)`, `list_for_org(org_id, filters, sort, cursor, limit)`
- `soft_delete(doc, deleted_at)`, `restore(doc)`, `update_metadata(doc, **fields)`
- `update_current_version_id(doc, version_id)`
- Keyset pagination on `(created_at, id)`, all filter predicates, soft-delete exclusion (`deleted_at IS NULL`)

`DocumentVersionRepository(BaseRepository[DocumentVersion])` with:
- `create(...)`, `get_for_document(document_id, version_number?)`, `list_for_document(document_id)`
- `get_max_version_number(document_id)`, `update_status(...)`

`CollectionRepository(TenantScopedRepository[Collection])` with:
- `create(...)`, `get_by_id_for_org(...)`, `list_for_org(org_id)`, `delete(...)`
- `add_document(collection_id, document_id)`, `remove_document(collection_id, document_id)`

---

### 6. Services

#### [NEW] [`app/services/document_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/document_service.py)

`DocumentService` — the core business orchestration layer:

**`upload_document()`** — exact transactional sequence from Backend §17.1:
1. Validate file (MIME allow-list, magic bytes, size limit)
2. Compute SHA-256 during streaming
3. Detect exact duplicate within same document's version history → `409`
4. Compute deterministic `storage_key`
5. **Stream bytes → Object Storage** (bytes durable FIRST, before any DB row)
6. `BEGIN` → `INSERT documents` (if new) + `INSERT document_versions(status=UPLOADED)` → `COMMIT`
7. Return `202 Accepted` with `document_id`, `version_id`

**`get_document()` / `list_documents()`** — with org-scoping, filters, pagination

**`download_document()`** — authorize → issue signed URL (5–15 min, never proxied)

**`soft_delete_document()` / `restore_document()`** — with audit events

**`update_document_metadata()`** — name/description/type/department/tags/access-level + audit on access-level change

**`bulk_action()`** — loop over single-doc service methods with per-item auth; returns per-item result list

**`upload_new_version()`** — `version_number = max+1` in transaction (constraint as backstop)

#### [NEW] [`app/services/collection_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/collection_service.py)

`CollectionService` — CRUD + membership management, `(organization_id, name)` uniqueness enforced.

---

### 7. Schemas (Pydantic)

#### [NEW] [`app/schemas/document.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/document.py)

Request/response models:
- `DocumentUploadResponse` (`document_id`, `version_id`, `status`)
- `DocumentResponse` (full metadata + current version + version count)
- `DocumentListResponse` (paginated, cursor-based)
- `DocumentMetadataUpdate` (PATCH body)
- `BulkActionRequest` / `BulkActionResponse` (per-item results)
- `VersionListResponse`
- `DownloadUrlResponse` (`url`, `expires_at`)
- `CollectionCreate`, `CollectionResponse`, `CollectionDocumentRequest`

---

### 8. API Router

#### [NEW] [`app/api/documents.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/documents.py)

All endpoints from the roadmap:

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/documents` | `document:create` | multipart upload; `documentId?` for new version |
| `GET` | `/documents` | `document:read` | list with filters/sort/pagination |
| `GET` | `/documents/{id}` | `document:read` | detail + current version |
| `PATCH` | `/documents/{id}` | `document:update` | metadata update |
| `DELETE` | `/documents/{id}` | `document:delete` | soft delete |
| `POST` | `/documents/{id}/restore` | `document:update` | restore from soft-delete |
| `GET` | `/documents/{id}/versions` | `document:read` | version history |
| `GET` | `/documents/{id}/download` | `document:read` | signed URL |
| `POST` | `/documents/bulk` | `document:update` | bulk actions |
| `GET` | `/collections` | `document:read` | list collections |
| `POST` | `/collections` | `document:create` | create collection |
| `DELETE` | `/collections/{id}` | `document:delete` | delete collection |
| `POST` | `/collections/{id}/documents` | `document:update` | add document to collection |
| `DELETE` | `/collections/{id}/documents/{doc_id}` | `document:update` | remove document from collection |

#### [MODIFY] [`app/main.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/main.py)

Register the `documents` router. Wire the real S3StorageProvider at lifespan startup (based on `settings.storage_provider`).

---

### 9. Audit Actions

#### [MODIFY] [`app/services/audit_logger.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/audit_logger.py)

Add document audit action constants: `DOCUMENT_UPLOADED`, `DOCUMENT_VIEWED` (session-debounced), `DOCUMENT_DOWNLOADED`, `DOCUMENT_DELETED`, `DOCUMENT_RESTORED`, `ACCESS_LEVEL_CHANGED`, `OWNERSHIP_TRANSFERRED`.

---

### 10. Configuration

#### [MODIFY] [`app/core/config.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/core/config.py)

Add:
- `max_upload_file_size_mb: int = 100` — org-configurable ceiling
- `signed_url_expires_seconds: int = 900` — 15 min default
- `storage_use_ssl: bool = False` — needed for MinIO local dev

---

### 11. Requirements

#### [MODIFY] [`requirements.txt`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/requirements.txt)

Add `python-magic==0.4.27` (magic-byte file-type detection). `boto3` is already present.

---

## Verification Plan

### Automated Tests

The existing test harness (testcontainers, pytest-asyncio, httpx) will be used:

```bash
cd backend && python -m pytest tests/ -v -k "document or collection"
```

- Upload validation: bad MIME, oversize, magic-byte mismatch → `400`/`413`
- Duplicate within document history → `409`
- Storage-outage upload attempt (mocked) → zero orphan DB rows (ordering rule)
- Cross-tenant document access → `404` (never `403` data leak)
- Listing with filters: type, status, owner, collection, department
- Keyset pagination correctness
- Soft delete → restore cycle
- Bulk partial-success shape
- Download: signed URL shape and expiry
- New version upload: `version_number = max+1`
- Collection CRUD + membership
- Audit events appear in `audit_logs` for each action

### Manual Verification

1. `docker compose up` (MinIO + Postgres + backend)
2. `alembic upgrade head` — migrations 001–004 run clean
3. Register org/user via `/auth/register`
4. Upload a PDF via `POST /documents` → get `202` with IDs
5. MinIO console confirms file exists at deterministic `storage_key`
6. `GET /documents` lists the document with `status=UPLOADED`
7. `GET /documents/{id}/download` returns a signed URL that downloads the file directly
8. Soft-delete and restore round-trip
9. Create collection, add document, list collection members

---

## Open Questions

> [!IMPORTANT]
> **Magic-byte library:** `python-magic` requires the `libmagic` C library to be installed on the OS (`apt-get install libmagic1` on Debian/Ubuntu, `brew install libmagic` on Mac, Windows needs the DLL). Do you have this on your dev machines/CI, or should I use the pure-Python `filetype` library instead (no native dependencies)?

> [!NOTE]
> **`CHUNKING` status:** The roadmap mentions adding `CHUNKING` to the version `status` enum (it appears in Backend §47 but is missing from the DB doc's explicit list). I'll include it in the CHECK constraint/migration now so Phase 4–6 don't require a schema migration for a simple status addition.

> [!NOTE]
> **`python-magic` on Windows:** Since you're on Windows, `python-magic-bin` (which bundles the DLL) may be preferable. I'll use the `filetype` library as it has zero native dependencies and works cross-platform, unless you prefer magic-byte sniffing via `python-magic`.
