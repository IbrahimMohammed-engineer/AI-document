/**
 * Command palette (Phase 15 §6.10).
 *
 * ⌘K / Ctrl+K opens (global listener lives in AppShell); Escape closes and
 * returns focus to the trigger. Sections:
 *   1. Documents         — client-side fuzzy filter over the document list
 *   2. Recent Conversations — client-side fuzzy filter over conversations
 *   3. Quick Actions     — static navigation actions
 *
 * Keyboard: ↑↓ navigate, Enter activates, Tab cycles sections.
 * ARIA: role="dialog" + aria-modal, aria-activedescendant tracks the focused
 * item, aria-live="polite" announces the result count.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useConversations } from '@/hooks/queries/useConversations'
import { useDocumentList } from '@/hooks/queries/useDocuments'
import { useUiStore } from '@/store/uiStore'
import './CommandPalette.css'

interface CommandItem {
  id: string
  section: 'documents' | 'conversations' | 'actions'
  label: string
  hint?: string
  activate: () => void
}

/** Case-insensitive subsequence fuzzy match — returns a score or null. */
function fuzzyScore(query: string, text: string): number | null {
  if (!query) return 0
  const haystack = text.toLowerCase()
  const needle = query.toLowerCase()
  let score = 0
  let index = 0
  for (const char of needle) {
    const found = haystack.indexOf(char, index)
    if (found === -1) return null
    score += found === index ? 2 : 1 // contiguous chars score higher
    index = found + 1
  }
  return score
}

export function CommandPalette() {
  const open = useUiStore((s) => s.commandPaletteOpen)
  const closeCommandPalette = useUiStore((s) => s.closeCommandPalette)
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLElement | null>(null)

  const [rawQuery, setRawQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)

  const documentsQuery = useDocumentList({ limit: 50 })
  const conversationsQuery = useConversations(10)

  useEffect(() => {
    if (open) {
      triggerRef.current = document.activeElement as HTMLElement
      setRawQuery('')
      setActiveIndex(0)
      // Focus the input after mount
      requestAnimationFrame(() => inputRef.current?.focus())
    } else {
      triggerRef.current?.focus?.()
    }
  }, [open])

  const activateItem = useCallback(
    (item: CommandItem) => {
      closeCommandPalette()
      item.activate()
    },
    [closeCommandPalette],
  )

  // ── Build items ───────────────────────────────────────────────────────────
  const items = useMemo<CommandItem[]>(() => {
    if (!open) return []
    const query = rawQuery.trim()

    const documentItems: CommandItem[] = (documentsQuery.data?.items ?? [])
      .map((doc): [CommandItem, number | null] => [
        {
          id: `doc-${doc.id}`,
          section: 'documents',
          label: doc.name,
          hint: doc.document_type,
          activate: () => navigate(`/app/documents/${doc.id}`),
        },
        fuzzyScore(query, `${doc.name} ${doc.document_type}`),
      ])
      .filter(([, score]) => score !== null)
      .sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0))
      .slice(0, 8)
      .map(([item]) => item)

    const conversationItems: CommandItem[] = (
      conversationsQuery.data?.items ?? []
    )
      .map((conversation): [CommandItem, number | null] => [
        {
          id: `conv-${conversation.id}`,
          section: 'conversations',
          label: conversation.title ?? 'Untitled conversation',
          activate: () => navigate(`/app/ask?conversation=${conversation.id}`),
        },
        fuzzyScore(query, conversation.title ?? 'untitled'),
      ])
      .filter(([, score]) => score !== null)
      .slice(0, 5)
      .map(([item]) => item)

    const actions: CommandItem[] = [
      { label: 'Upload Document', to: '/app/documents?upload=1' },
      { label: 'New Conversation', to: '/app/ask' },
      { label: 'Go to Search', to: '/app/search' },
      { label: 'Go to Research', to: '/app/research' },
      { label: 'Go to Analytics', to: '/app/analytics' },
      { label: 'Go to Settings', to: '/app/settings' },
    ]
      .map((action) => ({
        id: `action-${action.label}`,
        section: 'actions' as const,
        label: action.label,
        activate: () => navigate(action.to),
      }))
      .filter((item) => fuzzyScore(query, item.label) !== null)

    return [...documentItems, ...conversationItems, ...actions]
  }, [open, rawQuery, documentsQuery.data, conversationsQuery.data, navigate])

  useEffect(() => {
    setActiveIndex(0)
  }, [rawQuery])

  // Keep the active item in view
  useEffect(() => {
    const list = listRef.current
    if (!list) return
    list
      .querySelector(`[data-item-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  // ── Keyboard handling ─────────────────────────────────────────────────────
  const onKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case 'Escape':
        event.preventDefault()
        closeCommandPalette()
        break
      case 'ArrowDown':
        event.preventDefault()
        setActiveIndex((index) => Math.min(index + 1, items.length - 1))
        break
      case 'ArrowUp':
        event.preventDefault()
        setActiveIndex((index) => Math.max(index - 1, 0))
        break
      case 'Enter':
        event.preventDefault()
        if (items[activeIndex]) activateItem(items[activeIndex])
        break
      case 'Tab': {
        // Tab cycles between section heads: jump to the next section boundary
        event.preventDefault()
        const current = items[activeIndex]
        if (!current) break
        const sections: CommandItem['section'][] = ['documents', 'conversations', 'actions']
        const nextSection =
          sections[(sections.indexOf(current.section) + 1) % sections.length]
        const target = items.findIndex((item) => item.section === nextSection)
        if (target >= 0) setActiveIndex(target)
        break
      }
    }
  }

  if (!open) return null

  const sections: { key: CommandItem['section']; title: string }[] = [
    { key: 'documents', title: 'Documents' },
    { key: 'conversations', title: 'Recent Conversations' },
    { key: 'actions', title: 'Quick Actions' },
  ]

  let flatIndex = -1

  return (
    <div
      className="command-palette-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeCommandPalette()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="command-palette"
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          type="text"
          className="command-palette-input"
          placeholder="Search documents, conversations, actions…"
          value={rawQuery}
          onChange={(event) => setRawQuery(event.target.value)}
          aria-label="Search commands"
          aria-controls="command-palette-list"
          role="combobox"
          aria-expanded="true"
          aria-activedescendant={
            items[activeIndex] ? `command-item-${activeIndex}` : undefined
          }
        />

        <div
          ref={listRef}
          id="command-palette-list"
          className="command-palette-results"
          role="listbox"
          aria-label="Results"
        >
          <span className="sr-only" aria-live="polite">
            {items.length} result{items.length === 1 ? '' : 's'}
          </span>

          {items.length === 0 && (
            <div className="command-palette-empty">No matching results</div>
          )}

          {sections.map((section) => {
            const sectionItems = items.filter(
              (item) => item.section === section.key,
            )
            if (sectionItems.length === 0) return null
            return (
              <div key={section.key} className="command-palette-section">
                <div className="command-palette-section-title">{section.title}</div>
                {sectionItems.map((item) => {
                  flatIndex += 1
                  const index = flatIndex
                  const isActive = index === activeIndex
                  return (
                    <button
                      key={item.id}
                      id={`command-item-${index}`}
                      type="button"
                      role="option"
                      aria-selected={isActive}
                      data-item-index={index}
                      className={[
                        'command-palette-item',
                        isActive ? 'active' : '',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                      onMouseMove={() => setActiveIndex(index)}
                      onClick={() => activateItem(item)}
                    >
                      <span className="command-palette-item-label">{item.label}</span>
                      {item.hint && (
                        <span className="command-palette-item-hint">{item.hint}</span>
                      )}
                    </button>
                  )
                })}
              </div>
            )
          })}
        </div>

        <div className="command-palette-footer">
          <span>↑↓ navigate</span>
          <span>Enter open</span>
          <span>Esc close</span>
        </div>
      </div>
    </div>
  )
}
