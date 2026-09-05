/**
 * Analytics KPI snapshot (Phase 14 groundwork + Phase 15 range selector).
 *
 * Matches backend/app/api/analytics.py — including the Phase 15 optional
 * `?days=` range parameter (7/30/90; omitted = all time, backward compatible).
 * The full metrics/cost platform is Phase 19 scope.
 */

import { get } from './client'

export interface AnalyticsSummary {
  documentCount: number
  questionCount: number
  groundedAnswerPct: number
  citationCoveragePct: number
}

/** Requires the `analytics:read` permission. */
export async function getAnalyticsSummary(days?: number): Promise<AnalyticsSummary> {
  const suffix = days != null ? `?days=${days}` : ''
  return get<AnalyticsSummary>(`/analytics/summary${suffix}`)
}
