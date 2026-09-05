/**
 * Typed API functions for conflict detection (Phase 13).
 *
 * Matches backend/app/schemas/conflict.py and backend/app/api/conflicts.py.
 *
 * Endpoints consumed:
 *   GET  /conflicts                     → listConflicts()
 *   GET  /conflicts/{id}                → getConflict()
 *   POST /conflicts/{id}/resolve        → resolveConflict()
 *   GET  /conflicts/scan-status         → getConflictScanStatus()
 */

import { get, post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

export type ConflictStatus = 'OPEN' | 'REVIEWED' | 'DISMISSED'
export type ConflictSeverity = 'MAJOR' | 'MODERATE' | 'MINOR'
export type ConflictPriority = 'ACTIVE' | 'LIKELY_RESOLVED'
export type ConflictDetectionMethod =
  | 'BACKGROUND_SCAN'
  | 'COMPARISON_DERIVED'
  | 'RETRIEVAL_TIME'
export type VersionState = 'CURRENT' | 'SUPERSEDED' | 'SCHEDULED'
export type ConflictDecision = 'REVIEWED' | 'DISMISSED'

/**
 * One evidence statement. `version_state` is computed LIVE by the backend at
 * every read (never stored) — effective_date is a static display snapshot.
 */
export interface ConflictStatement {
  id: string
  document_id: string
  document_version_id: string
  document_name: string
  version_number: number
  chunk_id: string
  page_number: number
  section: string | null
  statement_text: string
  effective_date: string | null
  version_state: VersionState
}

/** Conflict summary row — from GET /conflicts. */
export interface ConflictSummary {
  id: string
  topic: string
  severity: ConflictSeverity
  status: ConflictStatus
  detection_method: ConflictDetectionMethod
  /** Computed live: ACTIVE only when every statement's version is CURRENT. */
  priority: ConflictPriority
  statement_count: number
  detected_at: string
}

/** Full conflict detail — embeds its evidence statements. */
export interface ConflictDetail extends ConflictSummary {
  statements: ConflictStatement[]
  resolved_by: string | null
  resolution_note: string | null
  resolved_at: string | null
}

export interface ConflictListResponse {
  items: ConflictSummary[]
}

/** Background-scan status snapshot. */
export interface ScanStatusResponse {
  last_scan_status:
    | 'PENDING'
    | 'PROCESSING'
    | 'RETRYING'
    | 'COMPLETED'
    | 'FAILED'
    | null
  last_scan_completed_at: string | null
  last_scan_conflicts_created: number | null
  /** Documents created after the last completed scan (partial-coverage banner). */
  unscanned_document_count: number
}

// ─── Request types ─────────────────────────────────────────────────────────────

export interface ListConflictsParams {
  status?: ConflictStatus
  severity?: ConflictSeverity
}

export interface ConflictResolveRequest {
  decision: ConflictDecision
  note?: string | null
}

// ─── API functions ─────────────────────────────────────────────────────────────

/**
 * List conflicts visible to the current user (source-document authorization
 * filters the result server-side).  Defaults to OPEN conflicts.
 */
export async function listConflicts(
  params?: ListConflictsParams,
): Promise<ConflictListResponse> {
  const query = new URLSearchParams()
  if (params?.status) query.set('status', params.status)
  if (params?.severity) query.set('severity', params.severity)
  const qs = query.toString()
  return get<ConflictListResponse>(`/conflicts${qs ? `?${qs}` : ''}`)
}

/** Get one conflict with its embedded evidence statements. */
export async function getConflict(conflictId: string): Promise<ConflictDetail> {
  return get<ConflictDetail>(`/conflicts/${conflictId}`)
}

/**
 * Apply the terminal resolution (REVIEWED or DISMISSED).
 * 409 when already resolved; 404 when nonexistent/unauthorized.
 */
export async function resolveConflict(
  conflictId: string,
  body: ConflictResolveRequest,
): Promise<ConflictDetail> {
  return post<ConflictDetail>(`/conflicts/${conflictId}/resolve`, body)
}

/** Background conflict-scan status for the current organization. */
export async function getConflictScanStatus(): Promise<ScanStatusResponse> {
  return get<ScanStatusResponse>('/conflicts/scan-status')
}
