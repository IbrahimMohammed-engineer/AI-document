/**
 * ScopeChips — Research Workspace left-panel scope selection (§6.5).
 *
 * "All documents" chip + one chip per document (multi-select). Mirrors the
 * chat scope model: no selection chips = whole knowledge base.
 */

import { useMemo } from 'react'

import { useDocumentList } from '@/hooks/queries/useDocuments'

export function ScopeChips({
  selectedIds,
  onSelectionChange,
}: {
  selectedIds: string[]
  onSelectionChange: (ids: string[]) => void
}) {
  const documentsQuery = useDocumentList({ limit: 50 })

  const chips = useMemo(
    () =>
      (documentsQuery.data?.items ?? []).map((doc) => ({
        id: doc.id,
        label: doc.name,
      })),
    [documentsQuery.data],
  )

  function toggle(id: string) {
    onSelectionChange(
      selectedIds.includes(id)
        ? selectedIds.filter((existing) => existing !== id)
        : [...selectedIds, id],
    )
  }

  const allSelected = selectedIds.length === 0

  return (
    <div className="scope-chips" role="group" aria-label="Search scope">
      <div className="scope-chip-row">
        <button
          type="button"
          className="scope-chip"
          aria-pressed={allSelected}
          onClick={() => onSelectionChange([])}
        >
          All documents
        </button>
        {chips.map((chip) => (
          <button
            key={chip.id}
            type="button"
            className="scope-chip"
            aria-pressed={selectedIds.includes(chip.id)}
            title={chip.label}
            onClick={() => toggle(chip.id)}
          >
            {chip.label}
          </button>
        ))}
      </div>
      {documentsQuery.isLoading && (
        <span className="text-xs text-muted">Loading documents…</span>
      )}
      {documentsQuery.isError && (
        <span className="text-xs text-muted">
          Could not load the document list — scope falls back to all documents.
        </span>
      )}
    </div>
  )
}
