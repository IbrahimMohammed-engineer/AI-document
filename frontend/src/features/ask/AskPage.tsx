/**
 * Ask AI screen skeleton (FE §6.6) — Phase 9.
 *
 * The first question→answer loop (vertical slice M4): chat window, scope
 * selector, staged loading labels ("Searching documents…" while retrieval
 * runs), a token-by-token streaming renderer with a stop control, the
 * Sources block, and the distinct "no grounded answer" state (FE §6.6's
 * trust mechanism — ungrounded answers never look like grounded ones).
 *
 * Conversation persistence (server-side) arrives in Phase 11 — the
 * transcript lives in component state and stop is client-side only.
 */

import { useCallback, useRef, useState } from 'react'
import { streamAsk, toAskScope, type AskSource, type AskHistoryTurn } from '@/lib/api/ask'
import { ScopeSelector, type ScopeMode } from './ScopeSelector'
import './ask.css'

// ─── Transcript model ─────────────────────────────────────────────────────────

interface AskTurn {
  id: string
  role: 'user' | 'assistant'
  /** user: the question. assistant: accumulated answer text. */
  content: string
  streaming?: boolean
  stopped?: boolean
  /** assistant turns */
  sources?: AskSource[]
  groundedness?: 'grounded' | 'partial' | 'ungrounded'
  note?: string | null
  /** failed turns keep the question visible with Retry (FE §6.6) */
  errorCode?: string
}

let _turnCounter = 0
function nextTurnId(): string {
  _turnCounter += 1
  return `turn-${Date.now()}-${_turnCounter}`
}

// ─── Component ────────────────────────────────────────────────────────────────

export function AskPage() {
  const [turns, setTurns] = useState<AskTurn[]>([])
  const [input, setInput] = useState('')
  const [phase, setPhase] = useState<'idle' | 'searching' | 'reading'>('idle')
  const [scopeMode, setScopeMode] = useState<ScopeMode>('all')
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const busy = phase !== 'idle'
  const scopeEmpty = scopeMode === 'selected' && selectedIds.length === 0

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    })
  }, [])

  const runAsk = useCallback(
    async (question: string, history: AskHistoryTurn[]) => {
      const controller = new AbortController()
      abortRef.current = controller
      setPhase('searching')

      const assistantId = nextTurnId()
      setTurns((prev) => [
        ...prev,
        { id: assistantId, role: 'assistant', content: '', streaming: true },
      ])

      const patchAssistant = (patch: Partial<AskTurn>) =>
        setTurns((prev) =>
          prev.map((t) => (t.id === assistantId ? { ...t, ...patch } : t)),
        )

      try {
        await streamAsk(
          { question, scope: toAskScope(scopeMode, selectedIds), history },
          {
            onEvent: (event) => {
              switch (event.type) {
                case 'token':
                  // First token → the stream has begun
                  setPhase('reading')
                  setTurns((prev) =>
                    prev.map((t) =>
                      t.id === assistantId
                        ? { ...t, content: t.content + event.text }
                        : t,
                    ),
                  )
                  scrollToBottom()
                  break
                case 'sources':
                  patchAssistant({ sources: event.sources })
                  break
                case 'done':
                  patchAssistant({
                    streaming: false,
                    groundedness: event.groundedness,
                    note: event.groundedness === 'ungrounded' ? event.message : null,
                  })
                  break
                case 'error':
                  patchAssistant({
                    streaming: false,
                    errorCode: event.code,
                    note: event.message,
                  })
                  break
              }
            },
          },
          controller.signal,
        )
        // Aborted mid-stream — freeze the partial text (FE §6.6 stop control)
        setTurns((prev) =>
          prev.map((t) =>
            t.id === assistantId && t.streaming
              ? { ...t, streaming: false, stopped: true }
              : t,
          ),
        )
      } catch (err) {
        if ((err as Error).name === 'AbortError') {
          setTurns((prev) =>
            prev.map((t) =>
              t.id === assistantId ? { ...t, streaming: false, stopped: true } : t,
            ),
          )
        } else {
          patchAssistant({
            streaming: false,
            errorCode: (err as { code?: string }).code ?? 'UNKNOWN_ERROR',
            note: (err as Error).message,
          })
        }
      } finally {
        abortRef.current = null
        setPhase('idle')
      }
    },
    [scopeMode, selectedIds, scrollToBottom],
  )

  function handleSubmit() {
    const question = input.trim()
    if (!question || busy || scopeEmpty) return

    setTurns((prev) => [...prev, { id: nextTurnId(), role: 'user', content: question }])
    setInput('')
    scrollToBottom()

    // Bounded recent history for follow-up rewriting (server re-bounds too)
    const history: AskHistoryTurn[] = turns
      .filter((t) => !t.errorCode && t.groundedness !== 'ungrounded')
      .slice(-4)
      .map((t) => ({ role: t.role, content: t.content }))

    void runAsk(question, history)
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  function handleRetry() {
    const lastUser = [...turns].reverse().find((t) => t.role === 'user')
    if (!lastUser || busy) return
    // Drop the failed assistant turn and re-ask the same question
    setTurns((prev) => {
      const idx = prev.findIndex((t) => t.role === 'assistant' && Boolean(t.errorCode))
      const withoutFailed = idx >= 0 ? prev.filter((_, i) => i !== idx) : prev
      return withoutFailed
    })
    const history: AskHistoryTurn[] = turns
      .filter((t) => t.role === 'user' || (!t.errorCode && t.groundedness !== 'ungrounded'))
      .slice(-4)
      .map((t) => ({ role: t.role, content: t.content }))
    void runAsk(lastUser.content, history)
  }

  const lastAssistantFailed = (() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const t = turns[i]
      if (t.role === 'assistant') return Boolean(t.errorCode)
    }
    return false
  })()

  return (
    <div className="ask-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Ask AI</h1>
          <p className="page-subtitle">
            Grounded answers from your documents — every source shown.
          </p>
        </div>
      </div>

      <ScopeSelector
        mode={scopeMode}
        selectedIds={selectedIds}
        onModeChange={setScopeMode}
        onSelectionChange={setSelectedIds}
      />

      <div className="card ask-window" ref={scrollRef} aria-live="polite">
        {turns.length === 0 && (
          <div className="empty-state ask-empty">
            <div className="empty-state-icon">✦</div>
            <div className="empty-state-title">Ask anything about your documents</div>
            <p className="empty-state-description">
              Try: "What is the approval process?" — answers cite the exact
              sources they came from.
            </p>
          </div>
        )}

        {turns.map((turn) =>
          turn.role === 'user' ? (
            <div key={turn.id} className="ask-turn ask-turn-user">
              <div className="ask-bubble ask-bubble-user">{turn.content}</div>
            </div>
          ) : (
            <div key={turn.id} className="ask-turn ask-turn-assistant">
              <div
                className={[
                  'ask-bubble',
                  'ask-bubble-assistant',
                  turn.groundedness === 'ungrounded' ? 'ask-bubble-ungrounded' : '',
                  turn.errorCode ? 'ask-bubble-error' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                {/* Staged loading label (FE §6.6): searching → reading → stream */}
                {turn.streaming && turn.content === '' && phase !== 'idle' && (
                  <div className="ask-status">
                    <span className="spinner spinner-sm" aria-hidden="true" />
                    {phase === 'searching'
                      ? 'Searching documents…'
                      : 'Reading sources…'}
                  </div>
                )}

                {turn.content && <div className="ask-answer">{turn.content}</div>}
                {turn.streaming && turn.content && (
                  <span className="ask-cursor" aria-hidden="true" />
                )}
                {turn.stopped && (
                  <div className="ask-note">Generation stopped — partial answer shown.</div>
                )}
                {turn.groundedness === 'ungrounded' && (
                  <div className="ask-ungrounded-badge" title="No supporting source found">
                    ⌀ No grounded answer found
                  </div>
                )}
                {turn.note && <div className="ask-note">{turn.note}</div>}
                {turn.errorCode && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={handleRetry}
                    disabled={lastAssistantFailed && busy}
                  >
                    Retry
                  </button>
                )}

                {turn.sources && turn.sources.length > 0 && (
                  <div className="ask-sources">
                    <div className="ask-sources-title">Sources</div>
                    {turn.sources.map((source) => (
                      <div key={source.index} className="ask-source">
                        <span className="ask-source-index">[{source.index}]</span>
                        <span className="ask-source-name">{source.document_name}</span>
                        <span className="ask-source-meta">
                          Page {source.page_number}
                          {source.section_title ? ` · ${source.section_title}` : ''}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ),
        )}
      </div>

      <div className="ask-input-row">
        <textarea
          className="ask-input"
          placeholder={
            scopeEmpty
              ? 'Select at least one document to ask a question.'
              : 'Ask a question…  (Enter to send, Shift+Enter for newline)'
          }
          value={input}
          rows={2}
          disabled={busy || scopeEmpty}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleSubmit()
            }
          }}
        />
        {busy ? (
          <button type="button" className="btn btn-secondary" onClick={handleStop}>
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleSubmit}
            disabled={!input.trim() || scopeEmpty}
          >
            Send
          </button>
        )}
      </div>
    </div>
  )
}
