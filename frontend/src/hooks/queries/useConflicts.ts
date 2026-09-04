/**
 * React Query hooks for conflict detection (Phase 13).
 *
 * - useConflicts          — list conflicts (status/severity filters)
 * - useConflict           — one conflict's detail (embedded statements)
 * - useResolveConflict    — mutation for the terminal resolution; invalidates
 *                           the list + detail + scan-status queries on success
 * - useConflictScanStatus — background-scan status snapshot (Dashboard KPI
 *                           + partial-coverage banner)
 * - useDocumentConflictSections — section ids of a document's open conflicts
 *                           (TOC markers — client-side filter over the open
 *                           list, V1 simplification per plan §21)
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

import {
  getConflict,
  getConflictScanStatus,
  listConflicts,
  resolveConflict,
  type ConflictDecision,
  type ConflictDetail,
  type ConflictListResponse,
  type ConflictResolveRequest,
  type ConflictSeverity,
  type ConflictStatus,
  type ScanStatusResponse,
} from '@/lib/api/conflicts'
import { useAuthStore } from '@/store/authStore'

// ─── Query keys ───────────────────────────────────────────────────────────────

export const conflictKeys = {
  list: (status?: ConflictStatus, severity?: ConflictSeverity) =>
    ['conflicts', 'list', status ?? 'all', severity ?? 'all'] as const,
  detail: (id: string) => ['conflicts', 'detail', id] as const,
  scanStatus: () => ['conflicts', 'scan-status'] as const,
}

// ─── useConflicts ─────────────────────────────────────────────────────────────

/**
 * Fetch the authorized conflict list.  Defaults to OPEN conflicts (the
 * backend's own default) — pass an explicit status for the Reviewed /
 * Dismissed tabs.
 */
export function useConflicts(
  status?: ConflictStatus,
  severity?: ConflictSeverity,
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ConflictListResponse>({
    queryKey: conflictKeys.list(status, severity),
    enabled: Boolean(accessToken),
    queryFn: () => listConflicts({ status, severity }),
    staleTime: 15_000,
  })
}

// ─── useConflict ──────────────────────────────────────────────────────────────

/** Fetch one conflict's detail (statements, live version states, priority). */
export function useConflict(conflictId: string | null | undefined) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ConflictDetail>({
    queryKey: conflictKeys.detail(conflictId ?? 'none'),
    enabled: Boolean(conflictId) && Boolean(accessToken),
    queryFn: () => getConflict(conflictId as string),
    staleTime: 15_000,
  })
}

// ─── useResolveConflict ───────────────────────────────────────────────────────

/**
 * Mutation: apply the terminal REVIEWED / DISMISSED resolution.
 * Invalidates every conflict query — the list tabs, the detail, and the
 * scan-status counters all refresh.
 */
export function useResolveConflict(conflictId: string) {
  const qc = useQueryClient()
  return useMutation<ConflictDetail, Error, ConflictResolveRequest>({
    mutationFn: (body) => resolveConflict(conflictId, body),
    onSuccess: (data) => {
      qc.setQueryData(conflictKeys.detail(conflictId), data)
      void qc.invalidateQueries({ queryKey: ['conflicts'] })
    },
  })
}

// Convenience wrapper carrying the decision type
export type ResolveConflictInput = { decision: ConflictDecision; note?: string | null }

// ─── useConflictScanStatus ────────────────────────────────────────────────────

/** Background-scan status (Dashboard "Open Conflicts" KPI + coverage banner). */
export function useConflictScanStatus() {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ScanStatusResponse>({
    queryKey: conflictKeys.scanStatus(),
    enabled: Boolean(accessToken),
    queryFn: getConflictScanStatus,
    staleTime: 30_000,
  })
}

// ─── useDocumentConflictSections ──────────────────────────────────────────────

/**
 * Section labels of the given document's OPEN conflicts — drives the TOC
 * `[•]` markers (FE §6.5).  Fetches the open-conflict list, then the details
 * of each (conflicts are few — plan §18's pagination reasoning), and collects
 * the `section` label of every statement whose document matches.
 */
export function useDocumentConflictSections(
  documentId: string | null | undefined,
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const openConflicts = useQuery<ConflictListResponse>({
    queryKey: conflictKeys.list('OPEN'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => listConflicts({ status: 'OPEN' }),
    staleTime: 30_000,
  })

  const items = openConflicts.data?.items ?? []
  const details = useQuery<Array<ConflictDetail | null>>({
    queryKey: ['conflicts', 'toc-details', items.map((c) => c.id).join(',')],
    enabled: items.length > 0 && Boolean(accessToken),
    queryFn: async () =>
      Promise.all(items.map((c) => getConflict(c.id).catch(() => null))),
    staleTime: 30_000,
  })

  const conflictSectionIds = useMemo(() => {
    if (!documentId) return new Set<string>()
    const ids = new Set<string>()
    for (const detail of details.data ?? []) {
      if (!detail) continue
      for (const statement of detail.statements) {
        if (statement.document_id === documentId && statement.section) {
          ids.add(statement.section)
        }
      }
    }
    return ids
  }, [details.data, documentId])

  return {
    conflictSectionIds,
    openConflictCount: items.length,
    isLoading: openConflicts.isLoading || details.isLoading,
  }
}
