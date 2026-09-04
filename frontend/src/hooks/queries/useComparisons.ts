/**
 * React Query hooks for document comparisons (Phase 12).
 *
 * - useComparison        — poll a comparison until COMPLETED or FAILED
 * - useComparisonChanges — list the detected changes (filtered by severity)
 * - useComparisonNarration — fetch the LLM-generated narrative
 * - useDocumentVersions  — version history for the version picker
 * - useInitiateComparison — mutation that creates/reuses a comparison
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getComparison,
  getComparisonNarration,
  initiateComparison,
  listComparisonChanges,
  type ChangeSeverity,
  type ComparisonChangesResponse,
  type ComparisonCreateResponse,
  type ComparisonNarrationResponse,
  type ComparisonResponse,
  type CompareRequest,
} from '@/lib/api/comparison'
import {
  listDocumentVersions,
  type DocumentVersionsResponse,
} from '@/lib/api/versions'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const comparisonKeys = {
  detail: (id: string) => ['comparisons', id] as const,
  changes: (id: string, severity?: ChangeSeverity) =>
    ['comparisons', id, 'changes', severity ?? 'all'] as const,
  narration: (id: string) => ['comparisons', id, 'narration'] as const,
}

export const versionKeys = {
  list: (documentId: string) => ['documents', documentId, 'versions'] as const,
}

// ─── Polling intervals ────────────────────────────────────────────────────────

/** Poll while the comparison is still running. */
const POLL_INTERVAL_MS = 3_000
/** Terminal statuses — stop polling. */
const TERMINAL = new Set(['COMPLETED', 'FAILED'])

// ─── useComparison ────────────────────────────────────────────────────────────

/**
 * Fetch and poll a comparison until it reaches a terminal state.
 *
 * Refetches every 3s while status is PENDING or PROCESSING.
 * Stops when COMPLETED or FAILED.
 */
export function useComparison(comparisonId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ComparisonResponse>({
    queryKey: comparisonKeys.detail(comparisonId ?? 'none'),
    enabled: Boolean(comparisonId) && Boolean(accessToken),
    queryFn: () => getComparison(comparisonId as string),
    staleTime: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status || TERMINAL.has(status)) return false
      return POLL_INTERVAL_MS
    },
  })
}

// ─── useComparisonChanges ─────────────────────────────────────────────────────

/**
 * Fetch the paginated list of changes for a completed comparison.
 * Optionally filter by severity.
 */
export function useComparisonChanges(
  comparisonId: string | null | undefined,
  severity?: ChangeSeverity,
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ComparisonChangesResponse>({
    queryKey: comparisonKeys.changes(comparisonId ?? 'none', severity),
    enabled: Boolean(comparisonId) && Boolean(accessToken),
    queryFn: () =>
      listComparisonChanges(comparisonId as string, severity ? { severity } : undefined),
    staleTime: 60_000,
  })
}

// ─── useComparisonNarration ───────────────────────────────────────────────────

/**
 * Fetch the LLM-generated narration for a completed comparison.
 * Only enabled when the comparison status is COMPLETED.
 */
export function useComparisonNarration(
  comparisonId: string | null | undefined,
  isCompleted: boolean,
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ComparisonNarrationResponse>({
    queryKey: comparisonKeys.narration(comparisonId ?? 'none'),
    enabled: Boolean(comparisonId) && Boolean(accessToken) && isCompleted,
    queryFn: () => getComparisonNarration(comparisonId as string),
    staleTime: 5 * 60_000, // Narrations don't change — keep 5min
  })
}

// ─── useDocumentVersions ──────────────────────────────────────────────────────

/**
 * Fetch the version history of a document for the version picker.
 */
export function useDocumentVersions(documentId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DocumentVersionsResponse>({
    queryKey: versionKeys.list(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => listDocumentVersions(documentId as string),
    staleTime: 30_000,
  })
}

// ─── useInitiateComparison ────────────────────────────────────────────────────

/**
 * Mutation to create or reuse a comparison.
 *
 * On success, pre-populates the comparison detail query cache so the
 * ComparisonPanel can immediately render the PENDING status.
 */
export function useInitiateComparison() {
  const qc = useQueryClient()
  return useMutation<ComparisonCreateResponse, Error, CompareRequest>({
    mutationFn: initiateComparison,
    onSuccess: (data) => {
      // Pre-populate the detail query cache — avoids a redundant fetch
      qc.setQueryData(comparisonKeys.detail(data.id), data)
    },
  })
}
