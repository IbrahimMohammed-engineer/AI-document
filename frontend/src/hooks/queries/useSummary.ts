/**
 * React Query hooks for document summaries (Phase 14).
 *
 * Mirrors useComparisons.ts exactly:
 * - useSummary           — poll a summary until COMPLETED or FAILED
 * - useRegenerateSummary — mutation; pre-populates the detail query cache
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getSummary,
  regenerateSummary,
  type SummaryResponse,
} from '@/lib/api/summary'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const summaryKeys = {
  detail: (documentId: string, version?: number) =>
    ['summaries', documentId, version ?? 'current'] as const,
}

// ─── Polling intervals ────────────────────────────────────────────────────────

/** Poll while the summary is still generating. */
const POLL_INTERVAL_MS = 3_000
/** Terminal statuses — stop polling. */
const TERMINAL = new Set(['COMPLETED', 'FAILED'])

// ─── useSummary ───────────────────────────────────────────────────────────────

/**
 * Fetch and poll the document's summary until it reaches a terminal state.
 *
 * The backend endpoint is get-or-create-and-poll: the FIRST call creates the
 * row + job (202), subsequent polls return the row (200) until COMPLETED.
 */
export function useSummary(
  documentId: string | null | undefined,
  version?: number,
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<SummaryResponse>({
    queryKey: summaryKeys.detail(documentId ?? 'none', version),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getSummary(documentId as string, version),
    staleTime: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status || TERMINAL.has(status)) return false
      return POLL_INTERVAL_MS
    },
  })
}

// ─── useRegenerateSummary ─────────────────────────────────────────────────────

/**
 * Mutation to regenerate a summary (requires `summary:regenerate`).
 *
 * On success, pre-populates the detail query cache with the returned
 * (now-pending) row so the page immediately shows the regenerating state.
 */
export function useRegenerateSummary() {
  const qc = useQueryClient()
  return useMutation<
    SummaryResponse,
    Error,
    { documentId: string; version?: number }
  >({
    mutationFn: ({ documentId, version }) =>
      regenerateSummary(documentId, version),
    onSuccess: (data, variables) => {
      qc.setQueryData(summaryKeys.detail(variables.documentId, variables.version), data)
    },
  })
}
