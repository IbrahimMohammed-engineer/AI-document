/**
 * React Query hooks for structured-information extraction (Phase 14).
 *
 * Mirrors useComparisons.ts's conventions:
 * - useExtractionRun    — poll a run until COMPLETED or FAILED
 * - useExtractionRuns   — paginated run history for a document
 * - useCreateExtractionRun — mutation creating a NEW run (audit trail)
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createExtractionRun,
  getExtractionRun,
  listExtractionRuns,
  type CreateExtractionRequest,
  type ExtractionRun,
  type ExtractionRunDetail,
  type ExtractionRunsResponse,
} from '@/lib/api/extraction'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const extractionKeys = {
  detail: (extractionId: string) => ['extractions', extractionId] as const,
  list: (documentId: string) => ['extractions', 'document', documentId] as const,
}

// ─── Polling intervals ────────────────────────────────────────────────────────

/** Poll while the run is still executing. */
const POLL_INTERVAL_MS = 3_000
/** Terminal statuses — stop polling. */
const TERMINAL = new Set(['COMPLETED', 'FAILED'])

// ─── useExtractionRun ─────────────────────────────────────────────────────────

/**
 * Fetch and poll one extraction run until it reaches a terminal state.
 * Items (grouped by category) arrive with the COMPLETED response.
 */
export function useExtractionRun(extractionId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ExtractionRunDetail>({
    queryKey: extractionKeys.detail(extractionId ?? 'none'),
    enabled: Boolean(extractionId) && Boolean(accessToken),
    queryFn: () => getExtractionRun(extractionId as string),
    staleTime: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status || TERMINAL.has(status)) return false
      return POLL_INTERVAL_MS
    },
  })
}

// ─── useExtractionRuns ────────────────────────────────────────────────────────

/**
 * Run history for a document (most recent first) — the auditable list.
 */
export function useExtractionRuns(
  documentId: string | null | undefined,
  params?: { limit?: number; offset?: number },
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ExtractionRunsResponse>({
    queryKey: [...extractionKeys.list(documentId ?? 'none'), params ?? {}],
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => listExtractionRuns(documentId as string, params),
    staleTime: 30_000,
  })
}

// ─── useCreateExtractionRun ───────────────────────────────────────────────────

/**
 * Mutation to create a NEW extraction run (every explicit call is a new
 * audit-trail entry).  On success, invalidates the run history and
 * pre-populates the run-detail cache.
 */
export function useCreateExtractionRun() {
  const qc = useQueryClient()
  return useMutation<ExtractionRun, Error, CreateExtractionRequest>({
    mutationFn: createExtractionRun,
    onSuccess: (data) => {
      qc.setQueryData(extractionKeys.detail(data.id), { ...data, items: null })
      if (data.document_id) {
        void qc.invalidateQueries({
          queryKey: extractionKeys.list(data.document_id),
        })
      }
    },
  })
}
