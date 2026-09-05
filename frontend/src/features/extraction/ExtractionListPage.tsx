/**
 * ExtractionListPage (Phase 14) — run history for a document.
 *
 * Table: run date, status, "View" link, and the "Run extraction" button
 * gated by `extraction:create` (mirrors the conflict pages' permission
 * gating via the auth store's advisory permissions).
 */

import { Link, useParams } from 'react-router-dom'

import { useDocument } from '@/hooks/queries/useDocuments'
import {
  useCreateExtractionRun,
  useExtractionRuns,
} from '@/hooks/queries/useExtractions'
import { useAuthStore } from '@/store/authStore'

import './extraction.css'

export function ExtractionListPage() {
  const { id } = useParams<{ id: string }>()
  const documentId = id ?? null

  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canCreate = Boolean(permissions?.includes('extraction:create'))

  const { data: document } = useDocument(documentId)
  const { data: runs, isLoading } = useExtractionRuns(documentId)
  const createRun = useCreateExtractionRun()

  return (
    <div className="extraction-list">
      <div className="page-header">
        <div>
          <div className="workspace-breadcrumb">
            <Link to={`/app/documents/${documentId ?? ''}`}>Document</Link>
            <span aria-hidden="true">/</span>
            <span>Extractions</span>
          </div>
          <h1 className="page-title">
            Extractions{document ? ` — ${document.name}` : ''}
          </h1>
          <p className="page-subtitle">
            Structured runs over this document's current content — every run
            is kept as an auditable record.
          </p>
        </div>
        {canCreate && documentId && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={createRun.isPending}
            onClick={() => createRun.mutate({ document_id: documentId })}
          >
            {createRun.isPending ? 'Starting…' : 'Run extraction'}
          </button>
        )}
      </div>

      {isLoading ? (
        <div className="page-loading">Loading runs…</div>
      ) : (runs?.items.length ?? 0) === 0 ? (
        <div className="card empty-state">
          <div className="empty-state-title">No extraction runs yet</div>
          <p className="empty-state-description">
            {canCreate
              ? 'Run an extraction to pull requirements, risks, dates, and parties out of this document — each item linked to its source text.'
              : 'Extraction runs will appear here once a user with the extraction permission starts one.'}
          </p>
        </div>
      ) : (
        <table className="extraction-runs-table">
          <thead>
            <tr>
              <th scope="col">Started</th>
              <th scope="col">Status</th>
              <th scope="col">Schema</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(runs?.items ?? []).map((run) => (
              <tr key={run.id}>
                <td>{new Date(run.created_at).toLocaleString()}</td>
                <td>
                  <span className={`status-badge status-badge--${run.status.toLowerCase()}`}>
                    {run.status}
                  </span>
                </td>
                <td>{run.schema_key}</td>
                <td>
                  <Link
                    to={`/app/documents/${run.document_id}/extractions/${run.id}`}
                    className="btn btn-secondary btn-sm"
                  >
                    View
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
