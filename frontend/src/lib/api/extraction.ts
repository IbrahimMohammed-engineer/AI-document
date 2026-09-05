/**
 * Typed API functions for structured-information extraction (Phase 14).
 *
 * Matches backend/app/schemas/extraction.py and backend/app/api/extractions.py.
 *
 * Endpoints consumed:
 *   POST /extractions                         → createExtractionRun()
 *   GET  /extractions/{extractionId}          → getExtractionRun()
 *   GET  /documents/{documentId}/extractions  → listExtractionRuns()
 */

import { get, post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

export type ExtractionStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
export type ExtractionCategory = 'requirement' | 'risk' | 'date' | 'party'

/** One extracted item with full citation-shaped provenance. */
export interface ExtractionItem {
  id: string
  category: ExtractionCategory
  item_index: number
  label: string
  detail: Record<string, unknown>
  chunk_id: string
  page_id: string
  page_number: number
  section: string | null
  quoted_text: string
  char_start: number | null
  char_end: number | null
  relevance_score: number | null
}

/** Items grouped by category — present only when status is COMPLETED. */
export interface ExtractionItemsByCategory {
  requirement: ExtractionItem[]
  risk: ExtractionItem[]
  date: ExtractionItem[]
  party: ExtractionItem[]
}

/** One extraction run (status + metadata). */
export interface ExtractionRun {
  id: string
  document_id: string
  document_version_id: string
  schema_key: string
  status: ExtractionStatus
  model: string | null
  prompt_version: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
}

/** Run detail — items included (grouped by category) when COMPLETED. */
export interface ExtractionRunDetail extends ExtractionRun {
  items: ExtractionItemsByCategory | null
}

/** Paginated run history for a document. */
export interface ExtractionRunsResponse {
  document_id: string
  total: number
  items: ExtractionRun[]
}

// ─── Request types ─────────────────────────────────────────────────────────────

export interface CreateExtractionRequest {
  document_id: string
  version?: number
  schema_key?: 'standard_v1'
}

// ─── API functions ─────────────────────────────────────────────────────────────

/**
 * Run structured extraction — always a NEW run (audit trail).  Requires the
 * `extraction:create` permission.  Poll `getExtractionRun()` until terminal.
 */
export async function createExtractionRun(
  body: CreateExtractionRequest,
): Promise<ExtractionRun> {
  return post<ExtractionRun>('/extractions', {
    schema_key: 'standard_v1',
    ...body,
  })
}

/**
 * Get one extraction run; items (grouped by category) are included when
 * status is COMPLETED.
 */
export async function getExtractionRun(
  extractionId: string,
): Promise<ExtractionRunDetail> {
  return get<ExtractionRunDetail>(`/extractions/${extractionId}`)
}

/**
 * Run history for a document, most recent first (auditable list).
 */
export async function listExtractionRuns(
  documentId: string,
  params?: { limit?: number; offset?: number },
): Promise<ExtractionRunsResponse> {
  const query = new URLSearchParams()
  if (params?.limit != null) query.set('limit', String(params.limit))
  if (params?.offset != null) query.set('offset', String(params.offset))
  const qs = query.toString()
  return get<ExtractionRunsResponse>(
    `/documents/${documentId}/extractions${qs ? `?${qs}` : ''}`,
  )
}
