/**
 * React Query hooks for background processing status (Phase 4 → Phase 11).
 *
 * Transport (Phase 11 — FE §11.2/§11.3): the SSE processing stream is the
 * live-updating optimization; REST remains the source of truth.
 *  - useDocumentStatus          — initial REST snapshot, then the SSE stream
 *    (`GET /documents/:id/stream`) writes every `status` event straight into
 *    the ['documents', id, 'status'] cache entry.  On stream failure the
 *    hook degrades to Phase 4's 3 s polling — transparent to consumers.
 *  - useProcessingJobs          — org-wide active jobs for the header
 *    indicator; while jobs are active a multiplexed stream
 *    (`GET /documents/stream?ids=…`) invalidates the list on every change
 *    (polling fallback on stream failure, as before).
 *  - useRetryProcessing         — the explicit FAILED → PROCESSING retry.
 *
 * Components consuming these hooks are unchanged: every status-driving UI
 * element is a pure function of the query cache (FE §11.5).
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getDocumentStatusApi,
  listProcessingJobsApi,
  retryProcessingApi,
  type DocumentRetryResponse,
  type DocumentStatusResponse,
  type ProcessingListResponse,
} from '@/lib/api/documents'
import { consumeSseStream, type SseEvent } from '@/lib/realtime'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const processingKeys = {
  status: (documentId: string) => ['documents', documentId, 'status'] as const,
  orgJobs: () => ['documents', 'processing'] as const,
}

// ─── SSE event handling (shared by both transports) ───────────────────────────

function applyStatusEvent(queryClient: ReturnType<typeof useQueryClient>) {
  return (event: SseEvent) => {
    if (event.event !== 'status') return
    const data = event.data as unknown as DocumentStatusResponse
    if (!data?.document_id) return
    // One cache write — every component reading this document's status
    // (table row, workspace header, upload row) updates in lockstep.
    queryClient.setQueryData(processingKeys.status(data.document_id), data)
    // The org-wide list reflects job progress too — refresh it lazily.
    queryClient.invalidateQueries({ queryKey: processingKeys.orgJobs() })
  }
}

/** Version/job states after which streaming/polling can stop. */
function isSettled(status: DocumentStatusResponse | undefined): boolean {
  if (!status) return false
  if (status.status === 'READY' || status.status === 'FAILED') return true
  // Phase 4's trivial pipeline ends with no live job — stop then too
  return status.job_status === null || status.job_status === 'COMPLETED'
}

// ─── Per-document status (SSE stream, polling fallback) ───────────────────────

export function useDocumentStatus(documentId: string | null | undefined) {
  const queryClient = useQueryClient()
  const accessToken = useAuthStore((s) => s.accessToken)
  const [streamFailed, setStreamFailed] = useState(false)

  const query = useQuery<DocumentStatusResponse>({
    queryKey: processingKeys.status(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentStatusApi(documentId as string),
    // Polling ONLY as the SSE fallback (FE §11.3); the stream handles updates.
    refetchInterval: (q) => {
      if (isSettled(q.state.data)) return false
      return streamFailed ? 3_000 : false
    },
    staleTime: 0,
  })

  const live = Boolean(documentId) && Boolean(accessToken) && !isSettled(query.data)

  useEffect(() => {
    if (!live || !documentId) {
      setStreamFailed(false)
      return
    }
    const controller = new AbortController()
    let cancelled = false

    const run = async () => {
      let attempt = 0
      while (!cancelled && attempt < 3) {
        try {
          await consumeSseStream(`/documents/${documentId}/stream`, {
            method: 'GET',
            signal: controller.signal,
            onEvent: (event) => {
              setStreamFailed(false)
              attempt = 0
              applyStatusEvent(queryClient)(event)
            },
          })
          // Stream closed server-side = every tracked document terminal.
          return
        } catch {
          if (cancelled || controller.signal.aborted) return
          attempt += 1
          setStreamFailed(true)
          // REST refetch on reconnect (FE §11.3 — events may have been missed)
          await queryClient.invalidateQueries({
            queryKey: processingKeys.status(documentId),
          })
          await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 5_000)))
        }
      }
      // SSE unavailable (e.g. corporate proxy) — stay on polling (FE §11.3)
    }

    void run()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [documentId, live, queryClient])

  return query
}

// ─── Org-wide active jobs (header indicator — multiplexed stream) ─────────────

export function useProcessingJobs() {
  const queryClient = useQueryClient()
  const accessToken = useAuthStore((s) => s.accessToken)
  const [streamFailed, setStreamFailed] = useState(false)

  const query = useQuery<ProcessingListResponse>({
    queryKey: processingKeys.orgJobs(),
    enabled: Boolean(accessToken),
    queryFn: () => listProcessingJobsApi(),
    // Low-frequency base refresh; the multiplexed stream provides the live
    // updates while jobs are active (5 s polling only on stream failure).
    refetchInterval: (q) => {
      const active = (q.state.data?.total ?? 0) > 0
      if (!active) return 30_000
      return streamFailed ? 5_000 : 30_000
    },
    staleTime: 0,
  })

  const activeIds = (query.data?.items ?? [])
    .map((item) => item.document_id)
    .filter((id, index, all) => all.indexOf(id) === index)
  const live = Boolean(accessToken) && activeIds.length > 0 && !streamFailed

  useEffect(() => {
    if (!live || activeIds.length === 0) {
      setStreamFailed(false)
      return
    }
    const controller = new AbortController()
    let cancelled = false

    const run = async () => {
      let attempt = 0
      while (!cancelled && attempt < 3) {
        try {
          await consumeSseStream(
            `/documents/stream?ids=${encodeURIComponent(activeIds.join(','))}`,
            {
              method: 'GET',
              signal: controller.signal,
              onEvent: (event) => {
                setStreamFailed(false)
                attempt = 0
                applyStatusEvent(queryClient)(event)
              },
            },
          )
          return // every tracked document reached a terminal state
        } catch {
          if (cancelled || controller.signal.aborted) return
          attempt += 1
          setStreamFailed(true)
          await queryClient.invalidateQueries({ queryKey: processingKeys.orgJobs() })
          await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 5_000)))
        }
      }
    }

    void run()
    return () => {
      cancelled = true
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, activeIds.join(','), queryClient])

  return query
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
