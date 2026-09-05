/**
 * ResearchTranscript — Research Workspace center panel (§6.5).
 *
 * Reuses the AskPage streaming logic (streamChatMessage SSE) and the shared
 * CitedAnswer/CitationList renderers — but citation clicks call
 * `panelStore.setActiveCitation()` (Journey 5: citation WITHOUT navigation).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  streamChatMessage,
  type ChatScopePayload,
  type ChatStreamEvent,
} from '@/lib/api/chat'
import type { AskCitation } from '@/lib/api/ask'
import { CitedAnswer } from '@/features/ask/CitedAnswer'
import { CitationList } from '@/features/ask/CitationList'
import { useStopMessage } from '@/hooks/queries/useConversations'
import { usePanelStore } from '@/store/panelStore'
import './research.css'

interface ResearchTurn {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  stopped?: boolean
  citations?: AskCitation[]
  groundedness?: 'grounded' | 'partial' | 'ungrounded'
  errorCode?: string
}

let _turnCounter = 0
function nextTurnId(): string {
  _turnCounter += 1
  return `research-turn-${Date.now()}-${_turnCounter}`
}

export function ResearchTranscript({
  conversationId,
  onConversationCreated,
}: {
  conversationId: string | null
  onConversationCreated: (id: string) => void
}) {
  const stopMessage = useStopMessage()
  const setActiveCitation = usePanelStore((s) => s.setActiveCitation)

  const [turns, setTurns] = useState<ResearchTurn[]>([])
  const [input, setInput] = useState('')
  const [phase, setPhase] = useState<'idle' | 'searching' | 'reading'>('idle')
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([])

  const abortRef = useRef<AbortController | null>(null)
  const assistantIdRef = useRef<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const busy = phase !== 'idle'

  // The workspace passes selected scope down via this setter
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<string[]>).detail
      setSelectedDocIds(detail ?? [])
    }
    window.addEventListener('research-scope-changed', handler)
    return () => window.removeEventListener('research-scope-changed', handler)
  }, [])

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    })
  }, [])

  const scopePayload = useCallback((): ChatScopePayload => {
    return selectedDocIds.length > 0
      ? { type: 'selected_documents', document_ids: selectedDocIds }
      : { type: 'knowledge_base' }
  }, [selectedDocIds])

  const runTurn = useCallback(
    async (question: string) => {
      const controller = new AbortController()
      abortRef.current = controller
      setPhase('searching')

      const userTurnId = nextTurnId()
      setTurns((prev) => [...prev, { id: userTurnId, role: 'user', content: question }])

      const patchAssistant = (id: string, patch: Partial<ResearchTurn>) =>
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)))

      let assistantId = nextTurnId()
      assistantIdRef.current = assistantId
      setTurns((prev) => [
        ...prev,
        { id: assistantId, role: 'assistant', content: '', streaming: true },
      ])

      const handleEvent = (event: ChatStreamEvent) => {
        switch (event.type) {
          case 'start':
            if (!conversationId && event.conversation_id) {
              onConversationCreated(event.conversation_id)
            }
            if (event.assistant_message_id) {
              const realId = event.assistant_message_id
              const placeholderId = assistantId
              assistantId = realId
              assistantIdRef.current = realId
              setTurns((prev) =>
                prev.map((t) => (t.id === placeholderId ? { ...t, id: realId } : t)),
              )
            }
            break
          case 'token':
            setPhase('reading')
            setTurns((prev) =>
              prev.map((t) =>
                t.id === assistantId ? { ...t, content: t.content + event.delta } : t,
              ),
            )
            scrollToBottom()
            break
          case 'citation':
            setTurns((prev) =>
              prev.map((t) =>
                t.id === assistantId
                  ? { ...t, citations: [...(t.citations ?? []), event.citation] }
                  : t,
              ),
            )
            break
          case 'done':
            patchAssistant(assistantId, {
              streaming: false,
              groundedness: event.groundedness,
              stopped: event.stopped,
              content: event.stopped && !event.answer ? '' : event.answer,
              citations: event.citations,
            })
            break
          case 'error':
            patchAssistant(assistantId, {
              streaming: false,
              errorCode: event.code,
            })
            break
        }
      }

      try {
        await streamChatMessage(
          conversationId,
          { content: question, scope: scopePayload() },
          { onEvent: handleEvent },
          controller.signal,
        )
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
          patchAssistant(assistantId, {
            streaming: false,
            errorCode: (err as { code?: string }).code ?? 'UNKNOWN_ERROR',
          })
        }
      } finally {
        abortRef.current = null
        assistantIdRef.current = null
        setPhase('idle')
      }
    },
    [conversationId, scopePayload, scrollToBottom, onConversationCreated],
  )

  function handleSubmit() {
    const question = input.trim()
    if (!question || busy) return
    setInput('')
    scrollToBottom()
    void runTurn(question)
  }

  function handleStop() {
    const assistantId = assistantIdRef.current
    if (assistantId && !assistantId.startsWith('research-turn-')) {
      stopMessage.mutate(assistantId)
    }
    abortRef.current?.abort()
  }

  function handleRetry() {
    const lastUser = [...turns].reverse().find((t) => t.role === 'user')
    if (!lastUser || busy) return
    setTurns((prev) => {
      const idx = prev.findIndex((t) => t.role === 'assistant' && Boolean(t.errorCode))
      return idx >= 0 ? prev.filter((_, i) => i !== idx) : prev
    })
    void runTurn(lastUser.content)
  }

  /** Journey 5 — citation click updates the EvidencePanel in place. */
  const handleCitationClick = useCallback(
    (citation: AskCitation) => {
      setActiveCitation(citation)
    },
    [setActiveCitation],
  )

  return (
    <>
      <div
        className="research-transcript"
        ref={scrollRef}
        aria-live="polite"
        aria-label="Research transcript"
      >
        {turns.length === 0 && (
          <div className="empty-state ask-empty">
            <div className="empty-state-icon" aria-hidden="true">
              ⛁
            </div>
            <div className="empty-state-title">Research across your documents</div>
            <p className="empty-state-description">
              Ask a question — citations open in the evidence panel without
              leaving your transcript.
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
                {turn.streaming && turn.content === '' && phase !== 'idle' && (
                  <div className="ask-status">
                    <span className="spinner spinner-sm" aria-hidden="true" />
                    {phase === 'searching' ? 'Searching documents…' : 'Reading sources…'}
                  </div>
                )}

                {turn.content && (
                  <CitedAnswer
                    text={turn.content}
                    citations={turn.citations ?? []}
                    onCitationClick={handleCitationClick}
                  />
                )}
                {turn.streaming && turn.content && (
                  <span className="ask-cursor" aria-hidden="true" />
                )}
                {turn.stopped && (
                  <div className="ask-note">Stopped — partial answer shown.</div>
                )}
                {turn.groundedness === 'ungrounded' && !turn.stopped && (
                  <div className="ask-ungrounded-badge" title="No supporting source found">
                    ⌀ No grounded answer found
                  </div>
                )}
                {turn.errorCode && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={handleRetry}
                  >
                    Retry
                  </button>
                )}

                {turn.citations && turn.citations.length > 0 && (
                  <CitationList
                    citations={turn.citations}
                    onCitationClick={handleCitationClick}
                  />
                )}
              </div>
            </div>
          ),
        )}
      </div>

      <div className="research-input-row">
        <textarea
          placeholder="Ask a research question…  (Enter to send, Shift+Enter for newline)"
          value={input}
          rows={2}
          disabled={busy}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              handleSubmit()
            }
          }}
          aria-label="Research question"
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
            disabled={!input.trim()}
          >
            Send
          </button>
        )}
      </div>
    </>
  )
}
