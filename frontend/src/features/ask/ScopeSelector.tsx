/**
 * Scope selector (FE §6.6) — always visible above the transcript.
 *
 * Phase 9 ships "Entire knowledge base" and "Selected documents" (the
 * current-document option appears only when Ask AI is launched from a
 * Document Workspace — Phase 11 wires that entry point).  The selection is
 * authoritative scope input; a document mentioned IN a question never
 * expands it (Backend §27/§29).
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { listDocumentsApi } from '@/lib/api/documents'
import { useAuthStore } from '@/store/authStore'

export type ScopeMode = 'all' | 'selected'

interface ScopeSelectorProps {
  mode: ScopeMode
  selectedIds: string[]
  onModeChange: (mode: ScopeMode) => void
  onSelectionChange: (ids: string[]) => void
}

export function ScopeSelector({
  mode,
  selectedIds,
  onModeChange,
  onSelectionChange,
}: ScopeSelectorProps) {
  const accessToken = useAuthStore((s) => s.accessToken)
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')

  const documentsQuery = useQuery({
    queryKey: ['documents', 'for-scope-selector'],
    queryFn: () => listDocumentsApi({ limit: 200 }),
    enabled: Boolean(accessToken),
    staleTime: 30_000,
  })

  const documents = useMemo(() => {
    const items = documentsQuery.data?.items ?? []
    const needle = filter.trim().toLowerCase()
    return needle ? items.filter((d) => d.name.toLowerCase().includes(needle)) : items
  }, [documentsQuery.data, filter])

  function toggle(id: string) {
    onSelectionChange(
      selectedIds.includes(id)
        ? selectedIds.filter((x) => x !== id)
        : [...selectedIds, id],
    )
  }

  const label =
    mode === 'all'
      ? 'Entire knowledge base'
      : `Selected documents (${selectedIds.length})`

  return (
    <div className="ask-scope">
      <span className="ask-scope-label">Scope:</span>
      <button
        type="button"
        className="ask-scope-chip"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {label} <span aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="ask-scope-panel card" role="group" aria-label="Document scope">
          <label className="ask-scope-option">
            <input
              type="radio"
              name="ask-scope-mode"
              checked={mode === 'all'}
              onChange={() => onModeChange('all')}
            />
            Entire knowledge base
            <span className="ask-scope-hint">(may take slightly longer)</span>
          </label>
          <label className="ask-scope-option">
            <input
              type="radio"
              name="ask-scope-mode"
              checked={mode === 'selected'}
              onChange={() => onModeChange('selected')}
            />
            Selected documents
          </label>

          {mode === 'selected' && (
            <>
              <input
                className="ask-scope-filter"
                type="search"
                placeholder="Filter documents…"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <div className="ask-scope-list">
                {documentsQuery.isLoading && <div className="text-muted text-sm">Loading documents…</div>}
                {!documentsQuery.isLoading && documents.length === 0 && (
                  <div className="text-muted text-sm">No documents found.</div>
                )}
                {documents.map((doc) => (
                  <label key={doc.id} className="ask-scope-doc">
                    <input
                      type="checkbox"
                      checked={selectedIds.includes(doc.id)}
                      onChange={() => toggle(doc.id)}
                    />
                    <span className="ask-scope-doc-name">{doc.name}</span>
                    <span className="badge badge-gray">{doc.document_type}</span>
                  </label>
                ))}
              </div>
            </>
          )}

          <button
            type="button"
            className="btn btn-primary btn-sm ask-scope-apply"
            onClick={() => setOpen(false)}
          >
            Apply
          </button>
        </div>
      )}
    </div>
  )
}
