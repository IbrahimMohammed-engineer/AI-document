/**
 * Audit Log page (Phase 15 §6.9) — filter bar + paginated table + CSV export.
 * Permission gate: settings:manage (matches the backend requirement).
 */

import { useMemo, useState } from 'react'

import { PermissionDenied } from '@/components/PermissionDenied'
import { useAuditLogs } from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import type { AuditLogItem } from '@/lib/api/settings'
import './settings.css'

const PAGE_SIZE = 50

/** Well-known audit actions for the filter select (seeded by prior phases). */
const ACTION_OPTIONS = [
  'USER_LOGIN',
  'USER_CREATED',
  'PERMISSION_CHANGED',
  'DOCUMENT_UPLOADED',
  'DOCUMENT_VIEWED',
  'DOCUMENT_DOWNLOADED',
  'DOCUMENT_DELETED',
  'QUESTION_ASKED',
  'CONFLICT_RESOLVED',
  'SUMMARY_REGENERATED',
  'EXTRACTION_RUN_CREATED',
]

function toCsv(rows: AuditLogItem[]): string {
  const header = ['timestamp', 'user_email', 'action', 'resource_type', 'resource_id', 'ip_address']
  const escape = (value: string | null) =>
    value == null ? '' : `"${String(value).replace(/"/g, '""')}"`
  const lines = rows.map((row) =>
    [
      row.created_at,
      row.user_email,
      row.action,
      row.resource_type,
      row.resource_id,
      row.ip_address,
    ]
      .map(escape)
      .join(','),
  )
  return [header.join(','), ...lines].join('\n')
}

export function AuditLogPage() {
  const permissionList = useAuthStore((s) => s.currentUser?.permissions)
  const permissions = useMemo(() => new Set(permissionList ?? []), [permissionList])

  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(0)

  // Empty strings must not be sent as filters
  const filters = {
    ...(action ? { action } : {}),
    ...(from ? { from: new Date(from).toISOString() } : {}),
    ...(to ? { to: new Date(`${to}T23:59:59`).toISOString() } : {}),
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  }

  const logsQuery = useAuditLogs(filters)

  if (!permissions.has('settings:manage')) {
    return <PermissionDenied />
  }

  const { data, isLoading, isError, refetch } = logsQuery
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  function exportCsv() {
    if (!data) return
    const blob = new Blob([toCsv(data.items)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Audit Log</h1>
          <p className="page-subtitle">
            Security-relevant events, newest first
          </p>
        </div>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={exportCsv}
          disabled={!data || data.items.length === 0}
        >
          Export CSV
        </button>
      </div>

      <div className="settings-filter-bar" role="group" aria-label="Audit log filters">
        <div className="settings-filter-field">
          <label htmlFor="audit-action">Action</label>
          <select
            id="audit-action"
            value={action}
            onChange={(event) => {
              setAction(event.target.value)
              setPage(0)
            }}
          >
            <option value="">All actions</option>
            {ACTION_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>
        <div className="settings-filter-field">
          <label htmlFor="audit-from">From</label>
          <input
            id="audit-from"
            type="date"
            value={from}
            onChange={(event) => {
              setFrom(event.target.value)
              setPage(0)
            }}
          />
        </div>
        <div className="settings-filter-field">
          <label htmlFor="audit-to">To</label>
          <input
            id="audit-to"
            type="date"
            value={to}
            onChange={(event) => {
              setTo(event.target.value)
              setPage(0)
            }}
          />
        </div>
      </div>

      {isLoading ? (
        <div className="card skeleton-card" aria-busy="true">
          <span className="skeleton" style={{ height: '2rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '2rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '2rem' }} />
        </div>
      ) : isError ? (
        <div className="card empty-state">
          <div className="empty-state-title">Could not load audit events</div>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      ) : data && data.items.length === 0 ? (
        <div className="card empty-state">
          <div className="empty-state-title">No matching events</div>
          <p className="empty-state-description">
            Adjust the filters to see more of the audit trail.
          </p>
        </div>
      ) : (
        <div className="card">
          <table className="settings-table">
            <thead>
              <tr>
                <th scope="col">Timestamp</th>
                <th scope="col">User</th>
                <th scope="col">Action</th>
                <th scope="col">Resource</th>
                <th scope="col">IP</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((item) => (
                <tr key={item.id}>
                  <td>{new Date(item.created_at).toLocaleString()}</td>
                  <td>{item.user_email ?? 'system'}</td>
                  <td>
                    <span className="permission-key">{item.action}</span>
                  </td>
                  <td>
                    {item.resource_type}
                    {item.resource_id ? ` · ${item.resource_id.slice(0, 8)}…` : ''}
                  </td>
                  <td>{item.ip_address ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="settings-pagination">
            <span>
              Page {page + 1} of {pageCount} · {total} events
            </span>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={page + 1 >= pageCount}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default AuditLogPage

