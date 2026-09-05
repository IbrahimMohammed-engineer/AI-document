/**
 * Research Workspace — the three-panel research experience (§6.5, Journey 5).
 *
 * ≥1280px: scope+conversations | question+transcript | evidence — all three
 * visible; side panels are draggable-resizable and persist in panelStore.
 * 1024–1279px: the left panel becomes a slide-out drawer.
 * <1024px: redirect to /app/ask (the two-panel fallback per §6.12).
 *
 * Citation clicks in the transcript set panelStore.activeCitation → the
 * EvidencePanel updates WITHOUT navigating away.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ScopeChips } from './ScopeChips'
import { ResearchTranscript } from './ResearchTranscript'
import { EvidencePanel } from './EvidencePanel'
import { useConversations } from '@/hooks/queries/useConversations'
import { usePanelStore } from '@/store/panelStore'
import './research.css'

/** Below this viewport width the Research Workspace redirects to Ask AI. */
const MIN_WORKSPACE_WIDTH = 1024

export function ResearchWorkspace() {
  const navigate = useNavigate()

  const leftWidth = usePanelStore((s) => s.leftWidth)
  const rightWidth = usePanelStore((s) => s.rightWidth)
  const leftCollapsed = usePanelStore((s) => s.leftCollapsed)
  const rightCollapsed = usePanelStore((s) => s.rightCollapsed)
  const setPanelWidth = usePanelStore((s) => s.setPanelWidth)
  const togglePanel = usePanelStore((s) => s.togglePanel)

  const [conversationId, setConversationId] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [drawerOpen, setDrawerOpen] = useState(false)

  const conversationsQuery = useConversations(20)
  const conversations = conversationsQuery.data?.items ?? []

  // ── Viewport guard (§6.5): < 1024px → /app/ask ────────────────────────────
  useEffect(() => {
    const check = () => {
      if (window.innerWidth < MIN_WORKSPACE_WIDTH) {
        navigate('/app/ask', { replace: true })
      }
    }
    check()
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [navigate])

  // ── Panel resizing (pointer-event drag handles) ───────────────────────────
  const draggingRef = useRef<'left' | 'right' | null>(null)

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      if (!draggingRef.current) return
      if (draggingRef.current === 'left') {
        setPanelWidth('left', event.clientX)
      } else {
        setPanelWidth('right', window.innerWidth - event.clientX)
      }
    }
    const onPointerUp = () => {
      draggingRef.current = null
      document.body.style.cursor = ''
      document
        .querySelectorAll('.research-resize-handle--active')
        .forEach((el) => el.classList.remove('research-resize-handle--active'))
    }
    document.addEventListener('pointermove', onPointerMove)
    document.addEventListener('pointerup', onPointerUp)
    return () => {
      document.removeEventListener('pointermove', onPointerMove)
      document.removeEventListener('pointerup', onPointerUp)
    }
  }, [setPanelWidth])

  function startDrag(panel: 'left' | 'right') {
    return (event: React.PointerEvent) => {
      draggingRef.current = panel
      document.body.style.cursor = 'col-resize'
      event.currentTarget.classList.add('research-resize-handle--active')
    }
  }

  const handleScopeChange = (ids: string[]) => {
    setSelectedIds(ids)
    window.dispatchEvent(new CustomEvent('research-scope-changed', { detail: ids }))
  }

  return (
    <div className="research-workspace">
      {/* ── Left panel: scope + conversation history ──────────────────────── */}
      {!leftCollapsed ? (
        <>
          <aside
            className={[
              'research-panel',
              'research-panel--left',
              drawerOpen ? 'research-drawer-open' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            style={{ width: leftWidth }}
            aria-label="Research scope and history"
          >
            <div className="research-panel-head">
              <h2 className="research-panel-title">Scope</h2>
              <button
                type="button"
                className="research-panel-toggle"
                onClick={() => togglePanel('left')}
                aria-label="Collapse scope panel"
                title="Collapse"
              >
                ⟨
              </button>
            </div>

            <ScopeChips selectedIds={selectedIds} onSelectionChange={handleScopeChange} />

            <div className="research-panel-head">
              <h2 className="research-panel-title">Conversations</h2>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => setConversationId(null)}
                disabled={false}
              >
                + New
              </button>
            </div>

            <div className="research-conversations">
              {conversationsQuery.isLoading && (
                <div className="text-sm text-muted">Loading…</div>
              )}
              {!conversationsQuery.isLoading && conversations.length === 0 && (
                <div className="text-sm text-muted">No conversations yet.</div>
              )}
              <ul className="scope-chip-list">
                {conversations.map((conversation) => (
                  <li key={conversation.id}>
                    <button
                      type="button"
                      className={`scope-chip${conversation.id === conversationId ? ' active' : ''}`}
                      aria-pressed={conversation.id === conversationId}
                      title={conversation.title ?? 'Untitled conversation'}
                      onClick={() => setConversationId(conversation.id)}
                    >
                      {conversation.title ?? 'Untitled conversation'}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </aside>
          <div
            className="research-resize-handle"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize scope panel"
            onPointerDown={startDrag('left')}
          />
        </>
      ) : (
        <div className="research-restore-bar">
          <button
            type="button"
            className="research-panel-toggle"
            onClick={() => togglePanel('left')}
            aria-label="Expand scope panel"
            title="Scope"
          >
            ⟩
          </button>
        </div>
      )}

      {/* ── Center panel: question + transcript ───────────────────────────── */}
      <section className="research-panel research-panel--center" aria-label="Research transcript">
        <div className="research-panel-head">
          <h2 className="research-panel-title">Research</h2>
          {(leftCollapsed || rightCollapsed) && (
            <span className="text-xs text-muted">
              Citation evidence opens in the right panel
            </span>
          )}
        </div>

        <ResearchTranscript
          conversationId={conversationId}
          onConversationCreated={setConversationId}
        />
      </section>

      {/* ── Right panel: evidence ─────────────────────────────────────────── */}
      {!rightCollapsed ? (
        <>
          <div
            className="research-resize-handle"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize evidence panel"
            onPointerDown={startDrag('right')}
          />
          <aside
            className="research-panel research-panel--right"
            style={{ width: rightWidth }}
            aria-label="Evidence"
          >
            <div className="research-panel-head">
              <h2 className="research-panel-title">Evidence</h2>
              <button
                type="button"
                className="research-panel-toggle"
                onClick={() => togglePanel('right')}
                aria-label="Collapse evidence panel"
                title="Collapse"
              >
                ⟩
              </button>
            </div>
            <EvidencePanel />
          </aside>
        </>
      ) : (
        <div className="research-restore-bar">
          <button
            type="button"
            className="research-panel-toggle"
            onClick={() => togglePanel('right')}
            aria-label="Expand evidence panel"
            title="Evidence"
          >
            ⟨
          </button>
        </div>
      )}

      {/* Tablet drawer trigger (1024–1279px shows the left panel as a drawer) */}
      <button
        type="button"
        className="research-panel-toggle research-drawer-trigger-css"
        aria-label={drawerOpen ? 'Close scope drawer' : 'Open scope drawer'}
        aria-expanded={drawerOpen}
        onClick={() => setDrawerOpen((open) => !open)}
      >
        Scope
      </button>
    </div>
  )
}
