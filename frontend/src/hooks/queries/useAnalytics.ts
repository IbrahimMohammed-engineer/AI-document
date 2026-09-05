/**
 * React Query hook for the minimal analytics KPI snapshot (Phase 14, §6.7).
 */

import { useQuery } from '@tanstack/react-query'

import { getAnalyticsSummary, type AnalyticsSummary } from '@/lib/api/analytics'
import { useAuthStore } from '@/store/authStore'

export const analyticsKeys = {
  summary: () => ['analytics', 'summary'] as const,
}

export function useAnalyticsSummary(enabled: boolean) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<AnalyticsSummary>({
    queryKey: analyticsKeys.summary(),
    enabled: enabled && Boolean(accessToken),
    queryFn: getAnalyticsSummary,
    staleTime: 60_000,
  })
}
