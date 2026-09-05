/**
 * Typed API functions for document summaries (Phase 14).
 *
 * Matches backend/app/schemas/summary.py and backend/app/api/summaries.py.
 *
 * Endpoints consumed:
 *   GET  /summaries/{documentId}?version=  → getSummary()
 *   POST /summaries/{documentId}/regenerate → regenerateSummary()
 */

import { get, post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

export type SummaryStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'

/** One inline resolved citation attached to a summary item. */
export interface SummaryCitation {
  index: number
  chunk_id: string
  document_id: string
  document_version_id: string
  document_name: string
  page_id: string
  page_number: number
  section: string | null
  relevance: number
  quoted_text: string
  char_start: number | null
  char_end: number | null
}

/** One summary list item: clean display text + its resolved citations. */
export interface SummaryItem {
  text: string
  citations: SummaryCitation[]
}

/** The persisted sampling disclosure — sampling is disclosed, never silent. */
export interface SamplingDisclosure {
  sampled: boolean
  strategy: 'full' | 'section_diverse'
  included_section_ids: string[]
  excluded_section_count: number
}

/** The structured summary payload (all fields explicit). */
export interface SummaryPayload {
  executive_summary: SummaryItem[]
  key_points: SummaryItem[]
  dates: SummaryItem[]
  roles: SummaryItem[]
  requirements: SummaryItem[]
  risks: SummaryItem[]
  topics: string[]
}

/** Summary row — from GET /summaries/{documentId} or POST regenerate. */
export interface SummaryResponse {
  id: string
  document_id: string
  document_version_id: string
  status: SummaryStatus
  summary: SummaryPayload | null
  sampling: SamplingDisclosure | null
  model: string | null
  prompt_version: string | null
  stale: boolean
  error_message: string | null
  created_at: string
  completed_at: string | null
}

// ─── API functions ─────────────────────────────────────────────────────────────

/**
 * Get-or-create-and-poll in one endpoint: returns the current summary for
 * the document's current (or explicitly requested) version, creating and
 * enqueuing a SUMMARY job on first access.  Poll until
 * `status === 'COMPLETED' | 'FAILED'`.
 */
export async function getSummary(
  documentId: string,
  version?: number,
): Promise<SummaryResponse> {
  const query = new URLSearchParams()
  if (version != null) query.set('version', String(version))
  const qs = query.toString()
  return get<SummaryResponse>(`/summaries/${documentId}${qs ? `?${qs}` : ''}`)
}

/**
 * Regenerate the summary — always a fresh job; the row resets to PENDING
 * while the existing content stays visible.  Requires `summary:regenerate`.
 */
export async function regenerateSummary(
  documentId: string,
  version?: number,
): Promise<SummaryResponse> {
  return post<SummaryResponse>(`/summaries/${documentId}/regenerate`, {
    version: version ?? null,
  })
}
