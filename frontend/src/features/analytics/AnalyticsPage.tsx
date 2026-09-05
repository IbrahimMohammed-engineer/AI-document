/**
 * Minimal Analytics placeholder replacement (Phase 14 groundwork, §6.7).
 *
 * ONE KpiCard row (Documents, Questions, Grounded Answers %, Citation
 * Coverage %) reading GET /analytics/summary.  No charts, no time-range
 * selector, no cost breakdown, no CSV export — the full metrics platform is
 * explicitly Phase 15/19 scope.
 */

import { useAnalyticsSummary } from '@/hooks/queries/useAnalytics'
import { useAuthStore } from '@/store/authStore'

function KpiCard({
  label,
  value,
}: {
  label: string
  value: string | number | undefined
}) {
  return (
    <div
      className="card"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: '0.5rem',
        padding: '1rem 1.25rem',
      }}
    >
      <div
        style={{
          fontSize: '1.75rem',
          fontWeight: 700,
          color: 'var(--color-neutral-800)',
        }}
      >
        {value ?? '—'}
      </div>
      <div className="text-sm text-muted">{label}</div>
    </div>
  )
}

export function AnalyticsPage() {
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canRead = Boolean(permissions?.includes('analytics:read'))
  const { data, isLoading } = useAnalyticsSummary(canRead)

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Analytics</h1>
          <p className="page-subtitle">
            Usage and AI-quality snapshot — the full metrics platform arrives
            in a later phase.
          </p>
        </div>
      </div>

      {!canRead ? (
        <div className="card empty-state">
          <div className="empty-state-title">Not available</div>
          <p className="empty-state-description">
            You need the analytics permission to view this page.
          </p>
        </div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
            gap: '1rem',
          }}
        >
          <KpiCard label="Documents" value={isLoading ? undefined : data?.documentCount} />
          <KpiCard label="Questions" value={isLoading ? undefined : data?.questionCount} />
          <KpiCard
            label="Grounded Answers %"
            value={isLoading ? undefined : `${data?.groundedAnswerPct ?? 0}%`}
          />
          <KpiCard
            label="Citation Coverage %"
            value={isLoading ? undefined : `${data?.citationCoveragePct ?? 0}%`}
          />
        </div>
      )}
    </div>
  )
}
