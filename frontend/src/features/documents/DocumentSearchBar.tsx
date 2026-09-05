/**
 * In-document search bar (Phase 15 §6.4.4).
 *
 * Controlled by the DocumentWorkspace: match state lives with the viewer so
 * the text layer can render highlights. Previous/Next step through matches
 * (also bound to F3 / Shift+F3 by the workspace).
 */

import { useEffect, useRef } from 'react'

interface DocumentSearchBarProps {
  open: boolean
  query: string
  onQueryChange: (query: string) => void
  matchCount: number
  matchIndex: number
  onPrev: () => void
  onNext: () => void
  onClose: () => void
}

export function DocumentSearchBar({
  open,
  query,
  onQueryChange,
  matchCount,
  matchIndex,
  onPrev,
  onNext,
  onClose,
}: DocumentSearchBarProps) {
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  if (!open) return null

  function handleKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
    } else if (event.key === 'F3') {
      event.preventDefault()
      if (event.shiftKey) onPrev()
      else onNext()
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (event.shiftKey) onPrev()
      else onNext()
    }
  }

  return (
    <div className="document-search-bar" role="search" aria-label="Search in document">
      <input
        ref={inputRef}
        type="text"
        value={query}
        placeholder="Find in document…"
        aria-label="Find in document"
        onChange={(event) => onQueryChange(event.target.value)}
        onKeyDown={handleKeyDown}
      />
      <span className="document-search-count" aria-live="polite">
        {matchCount > 0 ? `${matchIndex + 1} / ${matchCount}` : query ? '0 / 0' : ''}
      </span>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={onPrev}
        disabled={matchCount === 0}
        aria-label="Previous match (Shift+F3)"
        title="Previous match (Shift+F3)"
      >
        ↑
      </button>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={onNext}
        disabled={matchCount === 0}
        aria-label="Next match (F3)"
        title="Next match (F3)"
      >
        ↓
      </button>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={onClose}
        aria-label="Close search"
        title="Close (Esc)"
      >
        ×
      </button>
    </div>
  )
}
