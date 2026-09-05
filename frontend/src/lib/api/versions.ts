/**
 * Typed API functions for document versions (Phase 12).
 *
 * Adds the version-history endpoint needed by the comparison version picker.
 *
 * Endpoint: GET /documents/{id}/versions → listDocumentVersions()
 *
 * The `state` field is computed server-side per §8.6 of the Phase 12 plan:
 *   CURRENT    — the version currently returned by resolve_current_version()
 *   SUPERSEDED — an older READY version displaced by a newer one
 *   SCHEDULED  — a READY version with an effective_date in the future
 */

import { get } from './client'
import type { VersionStatus } from './documents'

// ─── Types ────────────────────────────────────────────────────────────────────

/** Version state label computed by the backend (§8.6). */
export type VersionState = 'CURRENT' | 'SUPERSEDED' | 'SCHEDULED'

/**
 * Full version detail — item of GET /documents/{id}/versions.
 * Matches backend DocumentVersionDetail schema.
 */
export interface DocumentVersionDetail {
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
  document_id: string
  storage_key: string
  checksum_sha256: string | null
  /** Computed version state — null when status is not READY. */
  state: VersionState | null
}

/** GET /documents/{id}/versions response. */
export interface DocumentVersionsResponse {
  document_id: string
  total: number
  items: DocumentVersionDetail[]
}

// ─── API functions ─────────────────────────────────────────────────────────────

/**
 * Fetch the full version history of a document.
 *
 * Returns all versions (any status), ordered by version_number descending
 * (newest first).  The `state` field is populated for READY versions only.
 */
export async function listDocumentVersions(
  documentId: string,
): Promise<DocumentVersionsResponse> {
  return get<DocumentVersionsResponse>(`/documents/${documentId}/versions`)
}
