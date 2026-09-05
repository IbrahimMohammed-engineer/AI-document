/**
 * React Query hooks for documents (Phases 4–6 + Phase 15 library additions).
 *
 * Detail/page/TOC hooks accept an optional `version` for the Phase 15
 * version selector — omitting it keeps the historical "latest version"
 * behaviour (the backend resolves the current version when `?version=` is
 * absent, so old call sites are unaffected).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  deleteDocumentApi,
  getDocumentApi,
  getDocumentChunksApi,
  getDocumentContentApi,
  getDocumentListApi,
  getDocumentPagesApi,
  getDocumentTocApi,
  getDownloadUrlApi,
  uploadDocumentApi,
  type DocumentChunksResponse,
  type DocumentDetail,
  type DocumentListParams,
  type DocumentListResponse,
  type DocumentPageContentResponse,
  type DocumentPagesResponse,
  type DocumentTocResponse,
  type DownloadUrlResponse,
  type UploadDocumentParams,
} from '@/lib/api/documents'
import { listDocumentVersions, type DocumentVersionsResponse } from '@/lib/api/versions'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const documentKeys = {
  all: () => ['documents'] as const,
  list: (params: DocumentListParams) => ['documents', 'list', params] as const,
  detail: (documentId: string) => ['documents', documentId, 'detail'] as const,
  pages: (documentId: string, version?: number) =>
    ['documents', documentId, 'pages', version ?? 'latest'] as const,
  signedUrl: (documentId: string, version?: number) =>
    ['documents', documentId, 'signed-url', version ?? 'latest'] as const,
  toc: (documentId: string) => ['documents', documentId, 'toc'] as const,
  chunks: (documentId: string) => ['documents', documentId, 'chunks'] as const,
  content: (documentId: string, page: number) =>
    ['documents', documentId, 'content', page] as const,
}

export const versionKeys = {
  list: (documentId: string) => ['documents', documentId, 'versions'] as const,
}

// ─── Document list (Phase 15 library page + Dashboard widgets) ────────────────

export function useDocumentList(params: DocumentListParams = {}) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DocumentListResponse>({
    queryKey: documentKeys.list(params),
    enabled: Boolean(accessToken),
    queryFn: () => getDocumentListApi(params),
    staleTime: 30_000,
  })
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

// ─── Signed URL (PDF.js viewer + fallback) ────────────────────────────────────

export function useSignedUrl(
  documentId: string | null | undefined,
  options: { version?: number } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { version } = options
  return useQuery<DownloadUrlResponse>({
    queryKey: documentKeys.signedUrl(documentId ?? 'none', version),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () =>
      getDownloadUrlApi(documentId as string, version != null ? { version } : {}),
    // Signed URLs expire (15 min default) — treat as stale well before that
    staleTime: 10 * 60_000,
    gcTime: 10 * 60_000,
  })
}

// ─── Extracted pages ──────────────────────────────────────────────────────────

export function useDocumentPages(
  documentId: string | null | undefined,
  options: { refetchWhileProcessing?: boolean; version?: number } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { refetchWhileProcessing = true, version } = options

  return useQuery<DocumentPagesResponse>({
    queryKey: documentKeys.pages(documentId ?? 'none', version),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () =>
      getDocumentPagesApi(documentId as string, {
        limit: 500,
        ...(version != null ? { version } : {}),
      }),
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
  options: { pipelineActive?: boolean; version?: number } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { pipelineActive = false, version } = options

  return useQuery<DocumentTocResponse>({
    queryKey: [...documentKeys.toc(documentId ?? 'none'), version ?? 'latest'],
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () =>
      getDocumentTocApi(documentId as string, version != null ? version : undefined),
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

// ─── Page content (Phase 10 source panel + Phase 15 EvidencePanel) ───────────

export function useDocumentContent(
  documentId: string | null | undefined,
  page: number | null,
  options: { version?: number; enabled?: boolean } = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { version, enabled = true } = options

  return useQuery<DocumentPageContentResponse>({
    queryKey: [...documentKeys.content(documentId ?? 'none', page ?? 0), version ?? 'latest'],
    enabled:
      enabled && Boolean(documentId) && page != null && Boolean(accessToken),
    queryFn: () =>
      getDocumentContentApi(documentId as string, {
        page: page as number,
        ...(version != null ? { version } : {}),
      }),
    staleTime: 60_000,
    retry: false,
  })
}

// ─── Versions (Phase 15 — version selector; moved from useComparisons) ───────

/**
 * Fetch the version history of a document for the workspace version picker.
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

// ─── Mutations (Phase 15 — upload + delete) ──────────────────────────────────

export function useUploadDocument() {
  const queryClient = useQueryClient()
  return useMutation<
    Awaited<ReturnType<typeof uploadDocumentApi>>,
    Error,
    UploadDocumentParams
  >({
    mutationFn: uploadDocumentApi,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ['documents', 'list'],
      })
      void queryClient.invalidateQueries({
        queryKey: ['documents', 'processing'],
      })
    },
  })
}

export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (documentId: string) => deleteDocumentApi(documentId),
    onSuccess: (_data, documentId) => {
      void queryClient.invalidateQueries({ queryKey: ['documents', 'list'] })
      void queryClient.invalidateQueries({
        queryKey: ['documents', documentId, 'detail'],
      })
    },
  })
}
