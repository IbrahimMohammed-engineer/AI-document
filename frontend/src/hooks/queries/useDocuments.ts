/**
 * React Query hooks for document detail + extracted pages (Phase 5).
 *
 * - useDocument       — document detail (metadata + current version summary)
 * - useSignedUrl      — short-lived signed URL for the original file
 * - useDocumentPages  — extracted page text + OCR flags; polls lightly while
 *   the version is still mid-pipeline so the pages panel fills in as
 *   extraction progresses (roadmap Phase 5 frontend work)
 */

import { useQuery } from '@tanstack/react-query'

import {
  getDocumentApi,
  getDocumentChunksApi,
  getDocumentPagesApi,
  getDocumentTocApi,
  getDownloadUrlApi,
  type DocumentChunksResponse,
  type DocumentDetail,
  type DocumentPagesResponse,
  type DocumentTocResponse,
  type DownloadUrlResponse,
} from '@/lib/api/documents'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const documentKeys = {
  detail: (documentId: string) => ['documents', documentId, 'detail'] as const,
  pages: (documentId: string) => ['documents', documentId, 'pages'] as const,
  signedUrl: (documentId: string) => ['documents', documentId, 'signed-url'] as const,
  toc: (documentId: string) => ['documents', documentId, 'toc'] as const,
  chunks: (documentId: string) => ['documents', documentId, 'chunks'] as const,
}

// ─── Document detail ──────────────────────────────────────────────────────────

export function useDocument(documentId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DocumentDetail>({
    queryKey: documentKeys.detail(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentApi(documentId as string),
    staleTime: 15_000,
  })
}

// ─── Signed URL (viewer groundwork — FE §13) ─────────────────────────────────

export function useSignedUrl(documentId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DownloadUrlResponse>({
    queryKey: documentKeys.signedUrl(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDownloadUrlApi(documentId as string),
    // Signed URLs expire (15 min default) — treat as stale well before that
    staleTime: 10 * 60_000,
    gcTime: 10 * 60_000,
  })
}

// ─── Extracted pages ──────────────────────────────────────────────────────────

export function useDocumentPages(
  documentId: string | null | undefined,
  options: { refetchWhileProcessing?: boolean } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { refetchWhileProcessing = true } = options

  return useQuery<DocumentPagesResponse>({
    queryKey: documentKeys.pages(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentPagesApi(documentId as string, { limit: 500 }),
    staleTime: 0,
    refetchInterval: (query) => {
      if (!refetchWhileProcessing) return false
      const data = query.state.data
      if (!data) return false
      // Stop once the version settles (FAILED never gets more pages) and
      // once extraction has caught up to the final page count.
      if (data.status === 'READY' || data.status === 'FAILED') return false
      const caughtUp = data.page_count != null && data.total >= data.page_count
      return caughtUp ? false : 3_000
    },
  })
}

// ─── Table of contents (Phase 6) ─────────────────────────────────────────────

/**
 * Section tree for the workspace TOC panel. While the pipeline is still in
 * flight (`pipelineActive`), polls lightly so the tree appears as soon as
 * CHUNKING completes; once settled, the tree is final for this version.
 */
export function useDocumentToc(
  documentId: string | null | undefined,
  options: { pipelineActive?: boolean } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { pipelineActive = false } = options

  return useQuery<DocumentTocResponse>({
    queryKey: documentKeys.toc(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentTocApi(documentId as string),
    staleTime: 0,
    refetchInterval: pipelineActive ? 5_000 : false,
  })
}

// ─── Chunks (Phase 6 — debug tooling) ────────────────────────────────────────

/** Chunks of a version with full provenance (internal chunk-quality view). */
export function useDocumentChunks(
  documentId: string | null | undefined,
  options: { limit?: number; enabled?: boolean } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { limit = 50, enabled = true } = options

  return useQuery<DocumentChunksResponse>({
    queryKey: [...documentKeys.chunks(documentId ?? 'none'), { limit }],
    enabled: enabled && Boolean(documentId) && Boolean(accessToken),
    queryFn: () => getDocumentChunksApi(documentId as string, { limit }),
    staleTime: 30_000,
  })
}
