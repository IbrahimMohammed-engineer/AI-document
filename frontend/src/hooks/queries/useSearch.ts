/**
 * React Query hook for hybrid search (Phase 15 §6.6).
 *
 * - Debounced 300ms (the caller passes the raw input; the hook delays the
 *   request, not the input state)
 * - Disabled below 2 characters (no-request discipline from the plan)
 * - staleTime 30s — search results are stable enough for a short window
 */

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { searchDocuments, type SearchRequestBody } from '@/lib/api/search'
import { useAuthStore } from '@/store/authStore'

const DEBOUNCE_MS = 300

export interface UseSearchOptions {
  /** Debounce window override (tests use 0). */
  debounceMs?: number
  enabled?: boolean
}

export function useSearch(
  params: Omit<SearchRequestBody, 'query'> & { query: string },
  options: UseSearchOptions = {},
) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { debounceMs = DEBOUNCE_MS, enabled = true } = options

  // Debounce the full param object so mode/filter changes don't spam either
  const [debouncedParams, setDebouncedParams] = useState(params)

  useEffect(() => {
    if (debounceMs <= 0) {
      setDebouncedParams(params)
      return
    }
    const timer = window.setTimeout(() => setDebouncedParams(params), debounceMs)
    return () => window.clearTimeout(timer)
  }, [params, debounceMs])

  const queryText = debouncedParams.query.trim()
  const hasQuery = queryText.length >= 2

  return useQuery({
    queryKey: ['search', debouncedParams],
    enabled: enabled && hasQuery && Boolean(accessToken),
    queryFn: () => searchDocuments({ ...debouncedParams, query: queryText }),
    staleTime: 30_000,
    placeholderData: (previous) => previous,
  })
}
