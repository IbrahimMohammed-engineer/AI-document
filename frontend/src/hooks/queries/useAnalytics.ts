/**
 * React Query hook for the analytics KPI snapshot (Phase 14 + Phase 15 range).
 */

import { useQuery } from '@tanstack/react-query'

import { getAnalyticsSummary, type AnalyticsSummary } from '@/lib/api/analytics'
import { useAuthStore } from '@/store/authStore'

export const analyticsKeys = {
  summary: (days?: number) => ['analytics', 'summary', days ?? 'all'] as const,
}

/** `days` — the Phase 15 range selector (7 / 30 / 90); omit for all time. */
export function useAnalyticsSummary(enabled: boolean, days?: number) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<AnalyticsSummary>({
    queryKey: analyticsKeys.summary(days),
    enabled: enabled && Boolean(accessToken),
    queryFn: () => getAnalyticsSummary(days),
    staleTime: 300_000,
  })
}
