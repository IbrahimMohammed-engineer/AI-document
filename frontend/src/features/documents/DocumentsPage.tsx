/**
 * Documents library page (Phase 15 §6.3) — the first real /app/documents.
 *
 * Filter bar (search / type / status), grid⇄table toggle persisted in
 * uiStore, permission-gated Upload button (document:create → document:create
 * is the backend key "document:create" — the frontend checks the same key),
 * and the full state matrix: skeleton, empty (upload CTA / no matches),
 * error with Retry. Rows/cards link into the Document Workspace.
 */

import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { PermissionDenied } from '@/components/PermissionDenied'
import { ProcessingStatusBadge, UploadDialog } from '@/features/documents'
import { useDeleteDocument, useDocumentList } from '@/hooks/queries/useDocuments'
import { ApiError } from '@/lib/api/client'
import { useAuthStore } from '@/store/authStore'
import { useUiStore } from '@/store/uiStore'
import type { DocumentListItem, VersionStatus } from '@/lib/api/documents'
import './documentsPage.css'

const DOCUMENT_TYPES = [
  'policy',
  'procedure',
  'sop',
  'contract',
  'technical',
  'regulatory',
  'hr',
  'marketing',
  'other',
]

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.round(diffMs / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

export default function DocumentsPage() {
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canUpload = Boolean(permissions?.includes('document:create'))
  const canDelete = Boolean(permissions?.includes('document:delete'))

  const viewMode = useUiStore((s) => s.documentViewMode)
  const setViewMode = useUiStore((s) => s.setDocumentViewMode)

  const [searchParams] = useSearchParams()
  const [uploadOpen, setUploadOpen] = useState(searchParams.get('upload') === '1')
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('active')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const appliedSearch = search.trim()
  const listQuery = useDocumentList({
    limit: 60,
    ...(appliedSearch ? { search: appliedSearch } : {}),
    ...(typeFilter ? { document_type: typeFilter } : {}),
    ...(statusFilter ? { status: statusFilter as 'active' | 'archived' } : {}),
  })

  // Open the dialog right after navigation from the command palette
  useEffect(() => {
    if (searchParams.get('upload') === '1') setUploadOpen(true)
  }, [searchParams])

  const deleteDocument = useDeleteDocument()

  if (!permissions?.includes('document:read')) {
    return <PermissionDenied />
  }

  const { data, isLoading, isError, error, refetch } = listQuery
  const documents = data?.items ?? []
  const hasFilters = Boolean(appliedSearch || typeFilter)

  function handleDelete(documentId: string) {
    setActionError(null)
    deleteDocument.mutate(documentId, {
      onError: (err) => setActionError(err.message),
      onSettled: () => setDeletingId(null),
    })
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Documents</h1>
          <p className="page-subtitle">
            {data ? `${data.total} document${data.total === 1 ? '' : 's'}` : 'Document library'}
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <div className="view-toggle" role="group" aria-label="View mode">
            <button
              type="button"
              className={viewMode === 'grid' ? 'active' : ''}
              aria-pressed={viewMode === 'grid'}
              onClick={() => setViewMode('grid')}
            >
              Grid
            </button>
            <button
              type="button"
              className={viewMode === 'table' ? 'active' : ''}
              aria-pressed={viewMode === 'table'}
              onClick={() => setViewMode('table')}
            >
              Table
            </button>
          </div>
          {canUpload && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setUploadOpen(true)}
            >
              Upload
            </button>
          )}
        </div>
      </div>

      {/* Filter bar */}
      <div className="documents-filter-bar" role="group" aria-label="Document filters">
        <input
          type="search"
          className="documents-search"
          placeholder="Search by name…"
          aria-label="Search documents by name"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="Filter by type"
          value={typeFilter}
          onChange={(event) => setTypeFilter(event.target.value)}
        >
          <option value="">All types</option>
          {DOCUMENT_TYPES.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by status"
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value)}
        >
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
      </div>

      {actionError && (
        <div className="partial-ocr-warning" role="alert">
          {actionError}
        </div>
      )}

      {/* ── States ───────────────────────────────────────────────────────── */}
      {isLoading ? (
        viewMode === 'grid' ? (
          <div className="documents-grid" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="card skeleton-card">
                <span className="skeleton" style={{ height: '1.2rem', width: '70%', marginBottom: '0.75rem' }} />
                <span className="skeleton" style={{ height: '0.9rem', width: '45%', marginBottom: '0.5rem' }} />
                <span className="skeleton" style={{ height: '0.9rem', width: '30%' }} />
              </div>
            ))}
          </div>
        ) : (
          <div className="card skeleton-card" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <span key={i} className="skeleton" style={{ height: '1.6rem', marginBottom: '0.5rem' }} />
            ))}
          </div>
        )
      ) : isError ? (
        <div className="card empty-state">
          <div className="empty-state-title">Could not load documents</div>
          <p className="empty-state-description">
            {error instanceof ApiError ? error.message : 'Something went wrong.'}
          </p>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      ) : documents.length === 0 ? (
        hasFilters ? (
          <div className="card empty-state">
            <div className="empty-state-title">No documents match your filters</div>
            <p className="empty-state-description">
              Try clearing the search or choosing a different type.
            </p>
          </div>
        ) : (
          <div className="card empty-state">
            <div className="empty-state-icon" aria-hidden="true">
              📄
            </div>
            <div className="empty-state-title">No documents yet</div>
            <p className="empty-state-description">
              Upload your first document to start asking questions and
              generating summaries.
            </p>
            {canUpload && (
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => setUploadOpen(true)}
              >
                Upload your first document
              </button>
            )}
          </div>
        )
      ) : viewMode === 'grid' ? (
        <div className="documents-grid">
          {documents.map((doc) => (
            <DocumentCard
              key={doc.id}
              doc={doc}
              canDelete={canDelete}
              onDelete={() => setDeletingId(doc.id)}
            />
          ))}
        </div>
      ) : (
        <div className="card">
          <table className="settings-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Department</th>
                <th scope="col">Status</th>
                <th scope="col">Pages</th>
                <th scope="col">Updated</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td>
                    <Link to={`/app/documents/${doc.id}`} className="documents-name-link">
                      {doc.name}
                    </Link>
                  </td>
                  <td>{doc.document_type}</td>
                  <td>{doc.department ?? '—'}</td>
                  <td>
                    {doc.processing_status ? (
                      <ProcessingStatusBadge
                        documentId={doc.id}
                        status={doc.processing_status as VersionStatus}
                      />
                    ) : (
                      doc.status
                    )}
                  </td>
                  <td>{doc.page_count ?? '—'}</td>
                  <td>{relativeTime(doc.updated_at)}</td>
                  <td>
                    {canDelete && (
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => setDeletingId(doc.id)}
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Delete confirmation */}
      {deletingId && (
        <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && setDeletingId(null)}>
          <div
            className="modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-confirm-title"
          >
            <h2 id="delete-confirm-title" className="modal-title">
              Delete this document?
            </h2>
            <p className="text-sm text-muted">
              The document is archived and removed from search and AI answers.
            </p>
            <div className="modal-actions">
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => setDeletingId(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                style={{ background: 'var(--color-error)' }}
                onClick={() => handleDelete(deletingId)}
                disabled={deleteDocument.isPending}
              >
                {deleteDocument.isPending ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} />
    </div>
  )
}

function DocumentCard({
  doc,
  canDelete,
  onDelete,
}: {
  doc: DocumentListItem
  canDelete: boolean
  onDelete: () => void
}) {
  return (
    <div className="card document-card">
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.5rem' }}>
        <Link to={`/app/documents/${doc.id}`} className="documents-name-link">
          {doc.name}
        </Link>
        {doc.processing_status && (
          <ProcessingStatusBadge
            documentId={doc.id}
            status={doc.processing_status as VersionStatus}
          />
        )}
      </div>
      <div className="text-sm text-muted">
        {doc.document_type}
        {doc.department ? ` · ${doc.department}` : ''}
        {doc.page_count != null ? ` · ${doc.page_count} pages` : ''}
      </div>
      <div
        style={{
          marginTop: 'auto',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          paddingTop: '0.75rem',
        }}
      >
        <span className="text-xs text-muted">{relativeTime(doc.updated_at)}</span>
        {canDelete && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onDelete}>
            Delete
          </button>
        )}
      </div>
    </div>
  )
}
