/**
 * Typed API functions for document comparisons (Phase 12).
 *
 * Matches backend/app/schemas/comparison.py and backend/app/api/compare.py.
 *
 * Endpoints consumed:
 *   POST  /documents/compare                       → initiateComparison()
 *   GET   /documents/compare/{id}                  → getComparison()
 *   GET   /documents/compare/{id}/changes          → listComparisonChanges()
 *   GET   /documents/compare/{id}/narration        → getComparisonNarration()
 */

import { get, post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

export type ComparisonStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'
export type ChangeType = 'ADDED' | 'REMOVED' | 'MODIFIED'
export type ChangeSeverity = 'MAJOR' | 'MODERATE' | 'MINOR'
export type VersionState = 'CURRENT' | 'SUPERSEDED' | 'SCHEDULED'

/** Summary counts from the JSONB summary column (populated on COMPLETED). */
export interface ComparisonSummary {
  total: number
  major: number
  moderate: number
  minor: number
  alignment_degraded: boolean
}

/** Full comparison row — from GET /documents/compare/{id}. */
export interface ComparisonResponse {
  id: string
  organization_id: string
  document_a_version_id: string
  document_b_version_id: string
  /** Phase 15 — parent document IDs for "View source" deep links. */
  document_a_id: string | null
  document_b_id: string | null
  status: ComparisonStatus
  summary: ComparisonSummary | null
  error_message: string | null
  requested_by: string
  created_at: string
  completed_at: string | null
}

/** Response from POST /documents/compare — adds the `created` flag. */
export interface ComparisonCreateResponse extends ComparisonResponse {
  /** True when a new comparison was triggered; False when reusing an existing one. */
  created: boolean
}

/** Resolved provenance for one side of a change (backend SourceRef). */
export interface ComparisonSourceRef {
  document_id: string
  document_version_id: string
  document_name: string
  version_number: number
  page_number: number
  section: string | null
}

/** One detected change from GET /documents/compare/{id}/changes. */
export interface ComparisonChange {
  id: string
  comparison_id: string
  change_type: ChangeType
  severity: ChangeSeverity
  section: string | null
  old_chunk_id: string | null
  new_chunk_id: string | null
  old_text: string | null
  new_text: string | null
  truncated: boolean
  created_at: string
  old_source: ComparisonSourceRef | null
  new_source: ComparisonSourceRef | null
}

/** Paginated changes response. */
export interface ComparisonChangesResponse {
  comparison_id: string
  total: number
  items: ComparisonChange[]
}

/** LLM narration response. */
export interface ComparisonNarrationResponse {
  comparison_id: string
  narration: string
  llm_narrated: boolean
}

// ─── Request types ─────────────────────────────────────────────────────────────

export interface CompareRequest {
  document_a_version_id: string
  document_b_version_id: string
}

export interface ListChangesParams {
  severity?: ChangeSeverity
  section?: string
}

// ─── API functions ─────────────────────────────────────────────────────────────

/**
 * Initiate a comparison between two document versions.
 *
 * Idempotent — returns the existing comparison if one already exists for the
 * same pair.  Poll `getComparison()` until `status === 'COMPLETED'`.
 *
 * Returns 202 Accepted on first creation; the `created` field distinguishes
 * new vs. reused.
 */
export async function initiateComparison(
  body: CompareRequest,
): Promise<ComparisonCreateResponse> {
  return post<ComparisonCreateResponse>('/documents/compare', body)
}

/**
 * Get comparison status + summary.
 * Poll this until `status === 'COMPLETED'` or `status === 'FAILED'`.
 */
export async function getComparison(comparisonId: string): Promise<ComparisonResponse> {
  return get<ComparisonResponse>(`/documents/compare/${comparisonId}`)
}

/**
 * List detected changes for a comparison.
 * Only meaningful once status is 'COMPLETED'.
 */
export async function listComparisonChanges(
  comparisonId: string,
  params?: ListChangesParams,
): Promise<ComparisonChangesResponse> {
  const query = new URLSearchParams()
  if (params?.severity) query.set('severity', params.severity)
  if (params?.section) query.set('section', params.section)
  const qs = query.toString()
  return get<ComparisonChangesResponse>(
    `/documents/compare/${comparisonId}/changes${qs ? `?${qs}` : ''}`,
  )
}

/**
 * Get an LLM-generated narration of the comparison changes.
 * Only available when status is 'COMPLETED'.
 */
export async function getComparisonNarration(
  comparisonId: string,
): Promise<ComparisonNarrationResponse> {
  return get<ComparisonNarrationResponse>(
    `/documents/compare/${comparisonId}/narration`,
  )
}
