/**
 * ConflictsPage — the /app/conflicts route (Phase 13, FE §6.13).
 *
 * Status tabs (Open / Reviewed / Dismissed — resolved conflicts move to the
 * Reviewed tab), severity filter, empty state with the last-scan timestamp,
 * and the partial-coverage banner ("N recently uploaded documents haven't
 * been scanned yet") driven by GET /conflicts/scan-status.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { ConflictSeverity, ConflictStatus } from '@/lib/api/conflicts'
import {
  useConflictScanStatus,
  useConflicts,
} from '@/hooks/queries/useConflicts'
import { useAuthStore } from '@/store/authStore'
import { ConflictCard } from './ConflictCard'
import './conflicts.css'

const STATUS_TABS: Array<{ value: ConflictStatus; label: string }> = [
  { value: 'OPEN', label: 'Open' },
  { value: 'REVIEWED', label: 'Reviewed' },
  { value: 'DISMISSED', label: 'Dismissed' },
]

const SEVERITY_FILTERS: Array<{ value: ConflictSeverity | null; label: string }> = [
  { value: null, label: 'All' },
  { value: 'MAJOR', label: 'Major' },
  { value: 'MODERATE', label: 'Moderate' },
  { value: 'MINOR', label: 'Minor' },
]

export function ConflictsPage() {
  const navigate = useNavigate()
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canResolve = Boolean(permissions?.includes('conflict:resolve'))

  const [statusTab, setStatusTab] = useState<ConflictStatus>('OPEN')
  const [severity, setSeverity] = useState<ConflictSeverity | null>(null)

  const conflictsQuery = useConflicts(statusTab, severity ?? undefined)
  const scanStatusQuery = useConflictScanStatus()

  const items = conflictsQuery.data?.items ?? []
  const scanStatus = scanStatusQuery.data

  return (
    <div className="conflicts-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Conflicts</h1>
          <p className="page-subtitle">
            Contradictions detected across your knowledge base — review,
            compare the sources, and resolve.
          </p>
        </div>
      </div>

      {scanStatus && scanStatus.unscanned_document_count > 0 && (
        <div className="conflicts-banner" role="status">
          <span className="conflicts-banner__icon">⏳</span>
          <span>
            {scanStatus.unscanned_document_count} recently uploaded document
            {scanStatus.unscanned_document_count === 1 ? " hasn't" : "s haven't"} been
            scanned for conflicts yet — the nightly scan will cover{' '}
            {scanStatus.unscanned_document_count === 1 ? 'it' : 'them'}.
          </span>
        </div>
      )}

      <div className="conflicts-toolbar">
        <div className="conflicts-tabs" role="tablist" aria-label="Filter by status">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={statusTab === tab.value}
              className={[
                'conflicts-tab',
                statusTab === tab.value ? 'conflicts-tab--active' : '',
              ].join(' ')}
              onClick={() => setStatusTab(tab.value)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div
          className="conflicts-severity-filter"
          role="group"
          aria-label="Filter by severity"
        >
          {SEVERITY_FILTERS.map((filter) => (
            <button
              key={filter.label}
              type="button"
              className={[
                'conflicts-severity-btn',
                severity === filter.value ? 'conflicts-severity-btn--active' : '',
              ].join(' ')}
              onClick={() => setSeverity(filter.value)}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      {conflictsQuery.isLoading && (
        <div className="card conflicts-loading">Loading conflicts…</div>
      )}

      {!conflictsQuery.isLoading && items.length === 0 && (
        <div className="card empty-state">
          <div className="empty-state-icon">✓</div>
          <div className="empty-state-title">
            {statusTab === 'OPEN'
              ? 'No conflicts detected across your knowledge base'
              : `No ${statusTab.toLowerCase()} conflicts`}
          </div>
          <p className="empty-state-description">
            {scanStatus?.last_scan_completed_at
              ? `Last scan completed ${new Date(
                  scanStatus.last_scan_completed_at,
                ).toLocaleString()}`
              : 'The nightly background scan has not completed yet.'}
          </p>
        </div>
      )}

      <div className="conflicts-list">
        {items.map((conflict) => (
          <ConflictCard
            key={conflict.id}
            conflict={conflict}
            canResolve={canResolve}
            onOpen={() => navigate(`/app/conflicts/${conflict.id}`)}
          />
        ))}
      </div>
    </div>
  )
}
