/**
 * Search page (Phase 15 §6.6) — the first real /app/search.
 *
 * Mode toggle (semantic / keyword / hybrid) wired into the request, 300ms
 * debounced query with a 2-char minimum, collapsible filters, skeleton
 * loading, distinct empty states (no query / no results), error with retry.
 * Result clicks deep-link into the Document Workspace at the cited page.
 */

import { useState } from 'react'

import { SearchResultCard } from './SearchResultCard'
import { useSearch } from '@/hooks/queries/useSearch'
import type { SearchMode } from '@/lib/api/search'
import './search.css'

const MODES: { value: SearchMode; label: string; description: string }[] = [
  { value: 'hybrid', label: 'Hybrid', description: 'Vector + keyword fused (default)' },
  { value: 'semantic', label: 'Semantic', description: 'Meaning-based vector search' },
  { value: 'keyword', label: 'Keyword', description: 'Exact full-text matching' },
]

export default function SearchPage() {
  const [rawQuery, setRawQuery] = useState('')
  const [mode, setMode] = useState<SearchMode>('hybrid')
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [documentType, setDocumentType] = useState('')
  const [department, setDepartment] = useState('')

  const query = rawQuery.trim()

  const searchQuery = useSearch({
    query,
    mode,
    ...(documentType || department
      ? {
          filters: {
            ...(documentType ? { document_types: [documentType] } : {}),
            ...(department ? { department } : {}),
          },
        }
      : {}),
  })

  const results = searchQuery.data?.results ?? []
  const hasQuery = query.length >= 2

  return (
    <div className="search-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Search</h1>
          <p className="page-subtitle">
            Hybrid semantic + keyword search across your knowledge base
          </p>
        </div>
      </div>

      {/* Mode toggle */}
      <div className="search-mode-group" role="group" aria-label="Search mode">
        {MODES.map((option) => (
          <button
            key={option.value}
            type="button"
            className={`search-mode-button${mode === option.value ? ' active' : ''}`}
            aria-pressed={mode === option.value}
            title={option.description}
            onClick={() => setMode(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {/* Query input */}
      <input
        type="search"
        className="search-input"
        placeholder="Type to search your knowledge base…"
        aria-label="Search query"
        value={rawQuery}
        autoFocus
        onChange={(event) => setRawQuery(event.target.value)}
      />

      {/* Filters */}
      <div className="search-filters-row">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          aria-expanded={filtersOpen}
          onClick={() => setFiltersOpen((open) => !open)}
        >
          {filtersOpen ? 'Hide filters' : 'Filters'}
        </button>
        {searchQuery.data?.message && (
          <span className="text-xs text-muted">{searchQuery.data.message}</span>
        )}
      </div>

      {filtersOpen && (
        <div className="search-filters" role="group" aria-label="Search filters">
          <div className="settings-filter-field">
            <label htmlFor="search-type">Document type</label>
            <select
              id="search-type"
              value={documentType}
              onChange={(event) => setDocumentType(event.target.value)}
            >
              <option value="">Any</option>
              {['policy', 'procedure', 'sop', 'contract', 'technical', 'regulatory', 'hr', 'marketing', 'other'].map(
                (type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ),
              )}
            </select>
          </div>
          <div className="settings-filter-field">
            <label htmlFor="search-department">Department</label>
            <input
              id="search-department"
              type="text"
              value={department}
              onChange={(event) => setDepartment(event.target.value)}
            />
          </div>
        </div>
      )}

      {/* ── Result states ─────────────────────────────────────────────────── */}
      {!hasQuery ? (
        <div className="card empty-state">
          <div className="empty-state-icon" aria-hidden="true">
            ⌕
          </div>
          <div className="empty-state-title">Type to search your knowledge base</div>
          <p className="empty-state-description">
            At least 2 characters. Results rank by meaning (semantic), exact
            wording (keyword), or both (hybrid).
          </p>
        </div>
      ) : searchQuery.isFetching ? (
        <div aria-busy="true">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="card skeleton-card search-skeleton">
              <span className="skeleton" style={{ height: '1rem', width: '30%', marginBottom: '0.75rem' }} />
              <span className="skeleton" style={{ height: '1.6rem', width: '85%', marginBottom: '0.5rem' }} />
              <span className="skeleton" style={{ height: '0.9rem', width: '45%' }} />
            </div>
          ))}
        </div>
      ) : searchQuery.isError ? (
        <div className="card empty-state">
          <div className="empty-state-title">Search failed</div>
          <p className="empty-state-description">
            {(searchQuery.error as Error).message}
          </p>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void searchQuery.refetch()}
          >
            Retry
          </button>
        </div>
      ) : results.length === 0 ? (
        <div className="card empty-state">
          <div className="empty-state-title">No results for “{query}”</div>
          <p className="empty-state-description">
            Try a different phrasing or switch the search mode.
          </p>
        </div>
      ) : (
        <>
          <p className="text-sm text-muted" aria-live="polite" style={{ marginBottom: '0.75rem' }}>
            {searchQuery.data?.total} result{searchQuery.data?.total === 1 ? '' : 's'}
            {searchQuery.data?.used_reranker ? ' · reranked' : ''}
          </p>
          <div className="search-results">
            {results.map((result) => (
              <SearchResultCard key={result.chunk_id} result={result} query={query} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
