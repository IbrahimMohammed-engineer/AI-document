/**
 * Analytics page (Phase 14 slice + Phase 15 §6.8 expansion).
 *
 * Phase 15 additions:
 *  1. Range selector — 7d / 30d / 90d / All time (backend ?days= param)
 *  2. Quality metrics as CSS progress bars (no chart library)
 *  3. Per-widget independent error states (retry per card)
 *  4. "View as table" toggle — data rendered as <table> for screen readers
 *  5. Token/cost placeholder card ("Cost analytics coming in a later phase")
 *
 * The `analytics:read` permission gate is unchanged.
 */

import { useState } from 'react'

import { useAnalyticsSummary } from '@/hooks/queries/useAnalytics'
import { useAuthStore } from '@/store/authStore'
import { PermissionDenied } from '@/components/PermissionDenied'

type RangeDays = 7 | 30 | 90 | null

const RANGES: { label: string; days: RangeDays }[] = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: 'All time', days: null },
]

/** One KPI value — loading skeleton / error+retry / value, independently. */
function KpiValue({
  isLoading,
  isError,
  onRetry,
  value,
}: {
  isLoading: boolean
  isError: boolean
  onRetry: () => void
  value: string | number | undefined
}) {
  if (isLoading) {
    return <span className="skeleton" style={{ width: '3.5rem', height: '1.8rem' }} aria-hidden="true" />
  }
  if (isError) {
    return (
      <span style={{ color: 'var(--color-neutral-400)' }}>
        —{' '}
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={onRetry}
          aria-label="Retry loading this metric"
        >
          ↻
        </button>
      </span>
    )
  }
  return <>{value ?? '—'}</>
}

/** CSS-only progress bar for quality percentages. */
function QualityBar({ label, pct, max = 100 }: { label: string; pct: number; max?: number }) {
  const clamped = Math.max(0, Math.min(pct, max))
  return (
    <div className="analytics-quality-row">
      <span className="analytics-quality-label">{label}</span>
      <div
        className="analytics-quality-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={Math.round(clamped)}
        aria-label={label}
      >
        <div className="analytics-quality-fill" style={{ width: `${(clamped / max) * 100}%` }} />
      </div>
      <span className="analytics-quality-value">{pct.toFixed(1)}%</span>
    </div>
  )
}

export function AnalyticsPage() {
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canRead = Boolean(permissions?.includes('analytics:read'))

  const [range, setRange] = useState<RangeDays>(null)
  const [viewAsTable, setViewAsTable] = useState(false)

  const { data, isLoading, isError, refetch } = useAnalyticsSummary(canRead, range ?? undefined)

  if (!canRead) {
    return (
      <PermissionDenied message="You need the analytics permission to view this page." />
    )
  }

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
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {/* Range selector (§6.8.1) */}
          <div className="analytics-range" role="tablist" aria-label="Time range">
            {RANGES.map((option) => (
              <button
                key={option.label}
                type="button"
                role="tab"
                aria-selected={range === option.days}
                className={`analytics-range-tab${range === option.days ? ' active' : ''}`}
                onClick={() => setRange(option.days)}
              >
                {option.label}
              </button>
            ))}
          </div>
          {/* View as table (§6.8.4) */}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-pressed={viewAsTable}
            onClick={() => setViewAsTable((value) => !value)}
          >
            {viewAsTable ? 'View as cards' : 'View as table'}
          </button>
        </div>
      </div>

      {viewAsTable ? (
        <div className="card">
          <table className="settings-table">
            <caption className="sr-only">Analytics summary for the selected range</caption>
            <thead>
              <tr>
                <th scope="col">Metric</th>
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Documents</td>
                <td>
                  <KpiValue
                    isLoading={isLoading}
                    isError={isError}
                    onRetry={() => void refetch()}
                    value={data?.documentCount}
                  />
                </td>
              </tr>
              <tr>
                <td>Questions</td>
                <td>
                  <KpiValue
                    isLoading={isLoading}
                    isError={isError}
                    onRetry={() => void refetch()}
                    value={data?.questionCount}
                  />
                </td>
              </tr>
              <tr>
                <td>Grounded answers</td>
                <td>
                  <KpiValue
                    isLoading={isLoading}
                    isError={isError}
                    onRetry={() => void refetch()}
                    value={data ? `${data.groundedAnswerPct}%` : undefined}
                  />
                </td>
              </tr>
              <tr>
                <td>Citation coverage</td>
                <td>
                  <KpiValue
                    isLoading={isLoading}
                    isError={isError}
                    onRetry={() => void refetch()}
                    value={data ? `${data.citationCoveragePct}%` : undefined}
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
            gap: '1rem',
          }}
        >
          <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', padding: '1rem 1.25rem' }}>
            <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--color-neutral-800)' }}>
              <KpiValue
                isLoading={isLoading}
                isError={isError}
                onRetry={() => void refetch()}
                value={data?.documentCount}
              />
            </div>
            <div className="text-sm text-muted">Documents</div>
          </div>
          <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', padding: '1rem 1.25rem' }}>
            <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--color-neutral-800)' }}>
              <KpiValue
                isLoading={isLoading}
                isError={isError}
                onRetry={() => void refetch()}
                value={data?.questionCount}
              />
            </div>
            <div className="text-sm text-muted">Questions</div>
          </div>
        </div>
      )}

      {/* Quality section as CSS progress bars (§6.8.2) */}
      <section className="card" style={{ marginTop: '1.5rem', padding: '1.25rem' }}>
        <h2 className="section-title" style={{ marginBottom: '1rem' }}>
          Answer quality
        </h2>
        {isLoading ? (
          <div aria-busy="true">
            <span className="skeleton" style={{ height: '1.2rem', marginBottom: '0.5rem' }} />
            <span className="skeleton" style={{ height: '1.2rem' }} />
          </div>
        ) : isError ? (
          <div>
            <div className="text-sm text-muted" style={{ marginBottom: '0.5rem' }}>
              Could not load quality metrics.
            </div>
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refetch()}>
              Retry
            </button>
          </div>
        ) : data ? (
          <>
            <QualityBar label="Grounded answers" pct={data.groundedAnswerPct} />
            <QualityBar label="Citation coverage" pct={data.citationCoveragePct} />
          </>
        ) : null}
      </section>

      {/* Token/cost placeholder (§6.8.5) */}
      <section className="card empty-state" style={{ marginTop: '1.5rem' }}>
        <div className="empty-state-icon" aria-hidden="true">
          🔒
        </div>
        <div className="empty-state-title">Cost analytics coming in a later phase</div>
        <p className="empty-state-description">
          Token and cost breakdowns arrive with the observability platform
          (Phase 19).
        </p>
      </section>
    </div>
  )
}
