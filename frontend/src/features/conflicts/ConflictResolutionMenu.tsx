/**
 * ConflictResolutionMenu — the accessible resolution dropdown (Phase 13).
 *
 * FE §6.13's richer menu labels ("Not a conflict", "Escalate to owner",
 * "Superseded — archive") all map onto the backend's two-value terminal
 * decision enum (plan §16 simplification):
 *   - Not a conflict            → DISMISSED
 *   - Escalate to owner         → REVIEWED
 *   - Superseded — archive      → DISMISSED
 *
 * Accessibility (FE §6.13): role="menu" + keyboard navigation
 * (Enter/Space/ArrowDown to open, ArrowUp/Down to move, Escape to close,
 * Enter to choose), focus returned to the trigger on close.
 */

import { useEffect, useRef, useState } from 'react'

import type { ConflictDecision } from '@/lib/api/conflicts'

interface ResolutionOption {
  label: string
  description: string
  decision: ConflictDecision
}

const OPTIONS: ResolutionOption[] = [
  {
    label: 'Not a conflict',
    description: 'Dismiss — the statements do not actually contradict.',
    decision: 'DISMISSED',
  },
  {
    label: 'Escalate to owner',
    description: 'Mark reviewed and flag for the document owner.',
    decision: 'REVIEWED',
  },
  {
    label: 'Superseded — archive',
    description: 'Dismiss — a newer version already resolves this.',
    decision: 'DISMISSED',
  },
]

export function ConflictResolutionMenu({
  onResolve,
  busy,
}: {
  onResolve: (decision: ConflictDecision, note: string | null) => void
  busy?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [highlighted, setHighlighted] = useState(0)
  const [showNote, setShowNote] = useState(false)
  const [note, setNote] = useState('')
  const containerRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onClickAway = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onClickAway)
    return () => document.removeEventListener('mousedown', onClickAway)
  }, [open])

  const choose = (option: ResolutionOption) => {
    setOpen(false)
    setShowNote(false)
    triggerRef.current?.focus()
    onResolve(option.decision, note.trim() ? note.trim() : null)
    setNote('')
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
        event.preventDefault()
        setOpen(true)
        setHighlighted(0)
      }
      return
    }
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        setHighlighted((h) => (h + 1) % OPTIONS.length)
        break
      case 'ArrowUp':
        event.preventDefault()
        setHighlighted((h) => (h - 1 + OPTIONS.length) % OPTIONS.length)
        break
      case 'Escape':
        event.preventDefault()
        setOpen(false)
        triggerRef.current?.focus()
        break
      case 'Enter':
      case ' ':
        event.preventDefault()
        choose(OPTIONS[highlighted])
        break
    }
  }

  return (
    <div className="conflict-resolution" ref={containerRef} onKeyDown={onKeyDown}>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn-secondary btn-sm"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => setOpen((o) => !o)}
      >
        Resolve ▾
      </button>

      {open && (
        <div className="conflict-resolution__menu" role="menu" aria-label="Resolve conflict">
          {!showNote ? (
            OPTIONS.map((option, index) => (
              <button
                key={option.label}
                type="button"
                role="menuitem"
                className={[
                  'conflict-resolution__item',
                  index === highlighted ? 'conflict-resolution__item--active' : '',
                ].join(' ')}
                onMouseEnter={() => setHighlighted(index)}
                onClick={() => {
                  setShowNote(true)
                  setHighlighted(index)
                }}
              >
                <span className="conflict-resolution__item-label">{option.label}</span>
                <span className="conflict-resolution__item-desc">{option.description}</span>
              </button>
            ))
          ) : (
            <div className="conflict-resolution__note">
              <label className="conflict-resolution__note-label" htmlFor="conflict-note">
                {OPTIONS[highlighted].label} — add an optional note
              </label>
              <textarea
                id="conflict-note"
                className="conflict-resolution__note-input"
                value={note}
                rows={3}
                autoFocus
                placeholder="Why is this being resolved?"
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="conflict-resolution__note-actions">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => setShowNote(false)}
                >
                  Back
                </button>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  onClick={() => choose(OPTIONS[highlighted])}
                  disabled={busy}
                >
                  Confirm
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
