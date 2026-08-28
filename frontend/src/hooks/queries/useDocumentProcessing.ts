/**
 * React Query hooks for background processing status (Phase 4).
 *
 * Transport is POLLING (FE §11's first phase):
 *  - useDocumentStatus  — per-document snapshot; polls every 2s while the
 *    version is in-flight, stops once READY/FAILED (or no live job).
 *  - useProcessingJobs  — org-wide active jobs for the header indicator;
 *    polls every 5s while signed in.
 *  - useRetryProcessing — mutation for the explicit FAILED → PROCESSING retry.
 *
 * Phase 11 swaps the polling queries for the SSE stream — the components
 * consuming these hooks do not change.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getDocumentStatusApi,
  listProcessingJobsApi,
  retryProcessingApi,
  type DocumentRetryResponse,
  type DocumentStatusResponse,
  type ProcessingListResponse,
} from '@/lib/api/documents'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const processingKeys = {
  status: (documentId: string) => ['documents', documentId, 'status'] as const,
  orgJobs: () => ['documents', 'processing'] as const,
}

// ─── Per-document status (polling) ────────────────────────────────────────────

/** Version/job states after which polling can stop. */
function isSettled(status: DocumentStatusResponse | undefined): boolean {
  if (!status) return false
  if (status.status === 'READY' || status.status === 'FAILED') return true
  // Phase 4's trivial pipeline ends with no live job — stop polling then too
  return status.job_status === null || status.job_status === 'COMPLETED'
}

export function useDocumentStatus(documentId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DocumentStatusResponse>({
    queryKey: processingKeys.status(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentStatusApi(documentId as string),
    refetchInterval: (query) => (isSettled(query.state.data) ? false : 2_000),
    staleTime: 0,
  })
}

// ─── Org-wide active jobs (header indicator) ─────────────────────────────────

export function useProcessingJobs() {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ProcessingListResponse>({
    queryKey: processingKeys.orgJobs(),
    enabled: Boolean(accessToken),
    queryFn: () => listProcessingJobsApi(),
    // Poll only while something is actually running; otherwise every 30s
    refetchInterval: (query) => ((query.state.data?.total ?? 0) > 0 ? 5_000 : 30_000),
    staleTime: 0,
  })
}

// ─── Retry mutation ───────────────────────────────────────────────────────────

export function useRetryProcessing() {
  const queryClient = useQueryClient()
  return useMutation<DocumentRetryResponse, Error, string>({
    mutationFn: (documentId: string) => retryProcessingApi(documentId),
    onSuccess: (_data, documentId) => {
      // Status flips FAILED → PROCESSING immediately — refresh both views
      queryClient.invalidateQueries({ queryKey: processingKeys.status(documentId) })
      queryClient.invalidateQueries({ queryKey: processingKeys.orgJobs() })
    },
  })
}
