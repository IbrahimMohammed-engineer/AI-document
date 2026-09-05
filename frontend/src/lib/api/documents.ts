/**
 * Typed API functions for documents + background processing (Phase 4).
 *
 * Matches backend/app/schemas/document.py and backend/app/schemas/processing.py.
 * Processing-status endpoints are the Phase 4 polling transport; the SSE
 * upgrade (Phase 11) will consume the same response shapes.
 */

import { del, get, post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

/** Pipeline status of a document version (document_versions.status). */
export type VersionStatus =
  | 'UPLOADED'
  | 'PROCESSING'
  | 'EXTRACTING'
  | 'OCR'
  | 'CHUNKING'
  | 'EMBEDDING'
  | 'INDEXING'
  | 'READY'
  | 'FAILED'

export type JobStatus = 'PENDING' | 'PROCESSING' | 'RETRYING' | 'COMPLETED' | 'FAILED'

export type JobType =
  | 'EXTRACTION'
  | 'OCR'
  | 'CHUNKING'
  | 'EMBEDDING'
  | 'INDEXING'
  | 'COMPARISON'
  | 'SUMMARY'
  | 'CONFLICT_SCAN'
  | 'PURGE'

/** GET /documents/{id}/status — processing status snapshot (polling). */
export interface DocumentStatusResponse {
  document_id: string
  version_id: string
  version_number: number
  status: VersionStatus
  /** 0–100, populated by workers while a stage is running */
  progress: number | null
  /** e.g. "340/512 chunks embedded" */
  progress_message: string | null
  error_message: string | null
  /** job_type of the in-flight stage, e.g. EXTRACTION */
  current_step: JobType | null
  job_id: string | null
  job_status: JobStatus | null
}

/** One active job — item of GET /documents/processing. */
export interface ProcessingJobItem {
  job_id: string
  document_id: string
  document_name: string
  version_id: string
  version_number: number
  job_type: JobType
  status: JobStatus
  progress: number | null
  progress_message: string | null
  attempts: number
  max_attempts: number
  started_at: string | null
  created_at: string
}

/** GET /documents/processing — org-wide active jobs (header widget). */
export interface ProcessingListResponse {
  items: ProcessingJobItem[]
  total: number
}

/** POST /documents/{id}/retry — 202 Accepted. */
export interface DocumentRetryResponse {
  job_id: string
  job_type: JobType
  status: JobStatus
  version_status: VersionStatus
  message: string
}

// ─── Document detail + pages (Phase 5) ────────────────────────────────────────

/** Version summary embedded in document detail responses. */
export interface DocumentVersionSummary {
  id: string
  version_number: number
  version_label: string | null
  status: VersionStatus
  mime_type: string
  file_size_bytes: number
  page_count: number | null
  effective_date: string | null
  expiration_date: string | null
  created_at: string
  created_by: string
  error_message: string | null
}

/** GET /documents/{id} — document detail. */
export interface DocumentDetail {
  id: string
  organization_id: string
  name: string
  description: string | null
  document_type: string
  department: string | null
  status: string
  access_level: string
  owner_id: string
  current_version_id: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
  tags: string[]
  current_version: DocumentVersionSummary | null
  version_count: number
}

/** GET /documents/{id}/download — short-lived signed URL. */
export interface DownloadUrlResponse {
  url: string
  expires_at: string
  mime_type: string
  file_size_bytes: number
  version_number: number
}

/** Lightweight document — item of GET /documents (list views + pickers). */
export interface DocumentListItem {
  id: string
  name: string
  document_type: string
  department: string | null
  status: string
  access_level: string
  owner_id: string
  current_version_id: string | null
  created_at: string
  updated_at: string
  tags: string[]
  processing_status: string | null
  page_count: number | null
}

/** GET /documents — document list response envelope. */
export interface DocumentListResponse {
  items: DocumentListItem[]
  total: number
}

/** One extracted page — item of GET /documents/{id}/pages. */
export interface DocumentPageItem {
  page_number: number
  text: string
  ocr_used: boolean
  ocr_failed: boolean
  width: number | null
  height: number | null
}

/** GET /documents/{id}/pages — extracted pages of one version (Phase 5). */
export interface DocumentPagesResponse {
  document_id: string
  version_id: string
  version_number: number
  mime_type: string
  status: VersionStatus
  page_count: number | null
  total: number
  items: DocumentPageItem[]
}

/** POST /documents (multipart upload) — 202 Accepted. */
export interface DocumentUploadResponse {
  document_id: string
  version_id: string
  version_number: number
  status: string
  is_duplicate_warning: boolean
  duplicate_version_id: string | null
  job_id: string | null
}

/** GET /documents/{id}/content — one page for the citation source panel. */
export interface DocumentPageContentResponse {
  document_id: string
  version_id: string
  version_number: number
  status: VersionStatus
  page_number: number
  page_count: number | null
  text: string
  ocr_used: boolean
  ocr_failed: boolean
  width: number | null
  height: number | null
}

// ─── Table of contents + chunks (Phase 6) ─────────────────────────────────────

/** One node of the section tree — GET /documents/{id}/toc. */
export interface TocNode {
  id: string
  title: string
  section_number: string | null
  level: number
  start_page: number
  end_page: number | null
  sort_order: number
  children: TocNode[]
}

/** GET /documents/{id}/toc — section tree for one version (Phase 6). */
export interface DocumentTocResponse {
  document_id: string
  version_id: string
  version_number: number
  /** false = "No structure detected" (FE §6.5) — page navigation only */
  has_structure: boolean
  section_count: number
  items: TocNode[]
}

/** One chunk with provenance — item of GET /documents/{id}/chunks. */
export interface DocumentChunkItem {
  chunk_index: number
  content: string
  token_count: number
  content_hash: string
  section_id: string | null
  section_number: string | null
  heading_path: string[]
  start_page: number | null
  end_page: number | null
  contains_table: boolean
  contains_list: boolean
  forced_split: boolean
  has_embedding: boolean
}

/** GET /documents/{id}/chunks — chunks of one version (debug tooling). */
export interface DocumentChunksResponse {
  document_id: string
  version_id: string
  version_number: number
  status: VersionStatus
  total: number
  items: DocumentChunkItem[]
}

// ─── Endpoints ────────────────────────────────────────────────────────────────

/** Processing status of a document's latest version (poll every few seconds). */
export function getDocumentStatusApi(documentId: string): Promise<DocumentStatusResponse> {
  return get<DocumentStatusResponse>(`/documents/${documentId}/status`)
}

/** Org-wide active jobs — drives the global header processing indicator. */
export function listProcessingJobsApi(limit = 100): Promise<ProcessingListResponse> {
  return get<ProcessingListResponse>(`/documents/processing?limit=${limit}`)
}

/**
 * Explicit retry of a failed stage (FAILED → PROCESSING).
 * Retries are NEVER automatic — this is the deliberate user action.
 */
export function retryProcessingApi(documentId: string): Promise<DocumentRetryResponse> {
  return post<DocumentRetryResponse>(`/documents/${documentId}/retry`)
}

/** Document detail (metadata + current version summary). */
export function getDocumentApi(documentId: string): Promise<DocumentDetail> {
  return get<DocumentDetail>(`/documents/${documentId}`)
}

/** Short-lived signed URL to the original file (never proxied by the API). */
export function getDownloadUrlApi(
  documentId: string,
  params: { version?: number } = {},
): Promise<DownloadUrlResponse> {
  const suffix = params.version != null ? `?version=${params.version}` : ''
  return get<DownloadUrlResponse>(`/documents/${documentId}/download${suffix}`)
}

/** Extracted page text + per-page OCR flags for a version (Phase 5). */
export function getDocumentPagesApi(
  documentId: string,
  params: { offset?: number; limit?: number; version?: number } = {},
): Promise<DocumentPagesResponse> {
  const query = new URLSearchParams()
  if (params.offset != null) query.set('offset', String(params.offset))
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.version != null) query.set('version', String(params.version))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<DocumentPagesResponse>(`/documents/${documentId}/pages${suffix}`)
}

/** Section tree for a version (Phase 6 — workspace TOC panel). */
export function getDocumentTocApi(
  documentId: string,
  version?: number,
): Promise<DocumentTocResponse> {
  const suffix = version != null ? `?version=${version}` : ''
  return get<DocumentTocResponse>(`/documents/${documentId}/toc${suffix}`)
}

/** Chunks with full provenance (Phase 6 — internal chunk-debug tooling). */
export function getDocumentChunksApi(
  documentId: string,
  params: { offset?: number; limit?: number } = {},
): Promise<DocumentChunksResponse> {
  const query = new URLSearchParams()
  if (params.offset != null) query.set('offset', String(params.offset))
  if (params.limit != null) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<DocumentChunksResponse>(`/documents/${documentId}/chunks${suffix}`)
}

/** Document list — powers library views and pickers (e.g. Ask AI scope, Phase 9). */
export function listDocumentsApi(params: { limit?: number; offset?: number } = {}): Promise<DocumentListResponse> {
  const query = new URLSearchParams()
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<DocumentListResponse>(`/documents${suffix}`)
}

// ─── Library page additions (Phase 15) ────────────────────────────────────────

/** Filter/sort params for the Documents library page (backend list contract). */
export interface DocumentListParams {
  limit?: number
  offset?: number
  search?: string
  document_type?: string
  department?: string
  status?: 'active' | 'archived'
  /** Backend returns newest-first by default (sortDesc=true). */
  sortDesc?: boolean
}

/** GET /documents with the full filter set (Documents library, Phase 15). */
export function getDocumentListApi(params: DocumentListParams = {}): Promise<DocumentListResponse> {
  const query = new URLSearchParams()
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  if (params.search) query.set('search', params.search)
  if (params.document_type) query.set('type', params.document_type)
  if (params.department) query.set('department', params.department)
  if (params.status) query.set('status', params.status)
  if (params.sortDesc != null) query.set('sortDesc', String(params.sortDesc))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<DocumentListResponse>(`/documents${suffix}`)
}

export interface UploadDocumentParams {
  file: File
  name: string
  document_type: string
  department?: string
  description?: string
  access_level?: 'organization' | 'restricted' | 'private'
  /** Supply to add a new version to an existing document. */
  document_id?: string
}

/** POST /documents multipart upload — 202 Accepted (processing is async). */
export function uploadDocumentApi(params: UploadDocumentParams): Promise<DocumentUploadResponse> {
  const form = new FormData()
  form.append('file', params.file)
  form.append('name', params.name)
  form.append('document_type', params.document_type)
  if (params.department) form.append('department', params.department)
  if (params.description) form.append('description', params.description)
  if (params.access_level) form.append('access_level', params.access_level)
  if (params.document_id) form.append('documentId', params.document_id)
  return post<DocumentUploadResponse>('/documents', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

/** DELETE /documents/{id} — soft delete (202 Accepted). */
export function deleteDocumentApi(documentId: string): Promise<void> {
  return del<void>(`/documents/${documentId}`)
}

/** GET /documents/{id}/content — one page's content (citation source panel). */
export function getDocumentContentApi(
  documentId: string,
  params: { page: number; version?: number },
): Promise<DocumentPageContentResponse> {
  const query = new URLSearchParams({ page: String(params.page) })
  if (params.version != null) query.set('version', String(params.version))
  return get<DocumentPageContentResponse>(
    `/documents/${documentId}/content?${query.toString()}`,
  )
}
