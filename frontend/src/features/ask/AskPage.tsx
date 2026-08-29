/**
 * Ask AI screen (FE §6.6 + §6.7 citation experience) — Phase 11.
 *
 * The complete AI document assistant: persistent conversations (lazy
 * creation — the conversation is born on the first message, never before),
 * scope selector mirrored field-for-field onto the backend scope model
 * (mid-conversation changes arrive as visible SYSTEM markers in the
 * transcript), token-by-token streaming with the stop control calling the
 * server cancellation endpoint, citation badges on completion, and
 * thumbs-up/down feedback (one rating per user per message).
 *
 * Vertical slice history: Phase 9 (first question→answer loop), Phase 10
 * (validated citations), Phase 11 (conversations, stop, feedback).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  streamChatMessage,
  type ChatScopePayload,
  type ChatStreamEvent,
} from '@/lib/api/chat'
import type { AskCitation, AskSource } from '@/lib/api/ask'
import {
  useConversations,
  useConversation,
  useDeleteConversation,
  useRateMessage,
  useStopMessage,
} from '@/hooks/queries/useConversations'
import { ScopeSelector, type ScopeMode } from './ScopeSelector'
import { CitationList } from './CitationList'
import { CitedAnswer } from './CitedAnswer'
import './ask.css'

// ─── Transcript model ─────────────────────────────────────────────────────────

interface AskTurn {
  id: string
  role: 'user' | 'assistant' | 'system'
  /** user: the question. assistant: the answer text (validated once done). */
  content: string
  streaming?: boolean
  stopped?: boolean
  /** assistant turns */
  sources?: AskSource[]
  citations?: AskCitation[]
  groundedness?: 'grounded' | 'partial' | 'ungrounded'
  note?: string | null
  feedback?: -1 | 1 | null
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
  // ── Conversation state ────────────────────────────────────────────────
  const [conversationId, setConversationId] = useState<string | null>(null)
  const conversationsQuery = useConversations()
  const conversationQuery = useConversation(conversationId)
  const deleteConversation = useDeleteConversation()
  const stopMessage = useStopMessage()
  const rateMessage = useRateMessage()

  const [turns, setTurns] = useState<AskTurn[]>([])
  const [input, setInput] = useState('')
  const [phase, setPhase] = useState<'idle' | 'searching' | 'reading'>('idle')
  const [scopeMode, setScopeMode] = useState<ScopeMode>('all')
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  const abortRef = useRef<AbortController | null>(null)
  const assistantIdRef = useRef<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const busy = phase !== 'idle'
  const scopeEmpty = scopeMode === 'selected' && selectedIds.length === 0

  // Loading a persisted conversation replaces the local transcript (the
  // streaming turn is optimistic; the server transcript is the truth).
  useEffect(() => {
    if (!conversationId || conversationQuery.isLoading) return
    if (busy) return // never swap the transcript mid-stream
    const detail = conversationQuery.data
    if (!detail) return
    setTurns(
      detail.messages.map((m) => ({
        id: m.id,
        role: m.role.toLowerCase() as AskTurn['role'],
        content: m.content,
        groundedness: m.groundedness ?? undefined,
        stopped: m.stopped,
        citations: m.citations,
        feedback: m.my_feedback ?? null,
        note:
          m.role === 'SYSTEM'
            ? null
            : m.groundedness === 'ungrounded'
              ? 'No grounded answer found in the selected sources.'
              : null,
      })),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, conversationQuery.data, conversationQuery.isLoading])

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    })
  }, [])

  const scopePayload = useCallback((): ChatScopePayload => {
    return scopeMode === 'selected'
      ? { type: 'selected_documents', document_ids: selectedIds }
      : { type: 'knowledge_base' }
  }, [scopeMode, selectedIds])

  // ── The streamed turn ─────────────────────────────────────────────────

  const runTurn = useCallback(
    async (question: string) => {
      const controller = new AbortController()
      abortRef.current = controller
      setPhase('searching')

      const userTurnId = nextTurnId()
      setTurns((prev) => [
        ...prev,
        { id: userTurnId, role: 'user', content: question },
      ])

      const patchAssistant = (id: string, patch: Partial<AskTurn>) =>
        setTurns((prev) =>
          prev.map((t) => (t.id === id ? { ...t, ...patch } : t)),
        )

      let assistantId = nextTurnId()
      assistantIdRef.current = assistantId
      setTurns((prev) => [
        ...prev,
        { id: assistantId, role: 'assistant', content: '', streaming: true },
      ])

      const handleEvent = (event: ChatStreamEvent) => {
        switch (event.type) {
          case 'start': {
            // Lazy creation (DB §20): the conversation is born here
            if (!conversationId && event.conversation_id) {
              setConversationId(event.conversation_id)
            }
            // Re-point the placeholder assistant turn onto the pre-allocated
            // id — it is the stop endpoint's target (Backend §37).
            if (event.assistant_message_id) {
              const realId = event.assistant_message_id
              const placeholderId = assistantId
              assistantId = realId
              assistantIdRef.current = realId
              setTurns((prev) =>
                prev.map((t) =>
                  t.id === placeholderId ? { ...t, id: realId } : t,
                ),
              )
            }
            break
          }
          case 'token':
            setPhase('reading')
            setTurns((prev) =>
              prev.map((t) =>
                t.id === assistantId
                  ? { ...t, content: t.content + event.delta }
                  : t,
              ),
            )
            scrollToBottom()
            break
          case 'citation':
            setTurns((prev) =>
              prev.map((t) =>
                t.id === assistantId
                  ? {
                      ...t,
                      citations: [...(t.citations ?? []), event.citation],
                    }
                  : t,
              ),
            )
            break
          case 'done':
            patchAssistant(assistantId, {
              streaming: false,
              groundedness: event.groundedness,
              stopped: event.stopped,
              note:
                event.groundedness === 'ungrounded' && !event.stopped
                  ? event.answer || 'No grounded answer found.'
                  : null,
              // The VALIDATED text is the truth — the backend may have
              // stripped uncited claims or invalid markers.
              content: event.stopped && !event.answer ? '' : event.answer,
              citations: event.citations,
              sources: event.sources,
              feedback: null,
            })
            break
          case 'error':
            patchAssistant(assistantId, {
              streaming: false,
              errorCode: event.code,
              note: event.message,
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
        // Aborted mid-stream (client stop) — freeze the partial (FE §6.6);
        // the server freezes its copy via the stop endpoint.
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
            note: (err as Error).message,
          })
        }
      } finally {
        abortRef.current = null
        assistantIdRef.current = null
        setPhase('idle')
      }
    },
    [conversationId, scopePayload, scrollToBottom],
  )

  function handleSubmit() {
    const question = input.trim()
    if (!question || busy || scopeEmpty) return
    setInput('')
    scrollToBottom()
    void runTurn(question)
  }

  function handleStop() {
    // Server-side cancellation first (Backend §37) — the client then
    // freezes the partial immediately without waiting for confirmation.
    const assistantId = assistantIdRef.current
    if (assistantId && !assistantId.startsWith('turn-')) {
      stopMessage.mutate(assistantId)
    }
    abortRef.current?.abort()
  }

  function handleRetry() {
    const lastUser = [...turns].reverse().find((t) => t.role === 'user')
    if (!lastUser || busy) return
    // Drop the failed assistant turn and re-ask the same question
    setTurns((prev) => {
      const idx = prev.findIndex((t) => t.role === 'assistant' && Boolean(t.errorCode))
      return idx >= 0 ? prev.filter((_, i) => i !== idx) : prev
    })
    void runTurn(lastUser.content)
  }

  function handleFeedback(turnId: string, rating: -1 | 1) {
    const turn = turns.find((t) => t.id === turnId)
    if (!turn || turn.id.startsWith('turn-')) return // persisted rows only
    const next = turn.feedback === rating ? null : rating
    setTurns((prev) =>
      prev.map((t) => (t.id === turnId ? { ...t, feedback: next } : t)),
    )
    if (next !== null) {
      rateMessage.mutate({ messageId: turnId, rating: next })
    }
  }

  function handleNewConversation() {
    if (busy) return
    setConversationId(null)
    setTurns([])
  }

  function handleSelectConversation(id: string) {
    if (busy || id === conversationId) return
    setConversationId(id)
  }

  function handleDeleteConversation(id: string) {
    deleteConversation.mutate(id, {
      onSuccess: () => {
        if (id === conversationId) {
          setConversationId(null)
          setTurns([])
        }
      },
    })
  }

  const lastAssistantFailed = (() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      const t = turns[i]
      if (t.role === 'assistant') return Boolean(t.errorCode)
    }
    return false
  })()

  const conversations = conversationsQuery.data?.items ?? []

  return (
    <div className="ask-page ask-page-with-list">
      <aside className="ask-conversations" aria-label="Conversations">
        <button
          type="button"
          className="btn btn-primary btn-sm ask-new-conversation"
          onClick={handleNewConversation}
          disabled={busy}
        >
          + New conversation
        </button>
        <div className="ask-conversation-list">
          {conversationsQuery.isLoading && (
            <div className="text-muted text-sm">Loading…</div>
          )}
          {!conversationsQuery.isLoading && conversations.length === 0 && (
            <div className="text-muted text-sm ask-conversation-empty">
              No conversations yet.
            </div>
          )}
          {conversations.map((conversation) => (
            <div
              key={conversation.id}
              className={[
                'ask-conversation-item',
                conversation.id === conversationId ? 'active' : '',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <button
                type="button"
                className="ask-conversation-open"
                onClick={() => handleSelectConversation(conversation.id)}
                title={conversation.title ?? 'Untitled conversation'}
              >
                <span className="ask-conversation-title">
                  {conversation.title ?? 'Untitled conversation'}
                </span>
                <span className="ask-conversation-meta">
                  {new Date(conversation.updated_at).toLocaleDateString()}
                </span>
              </button>
              <button
                type="button"
                className="ask-conversation-delete"
                aria-label="Archive conversation"
                title="Archive"
                onClick={() => handleDeleteConversation(conversation.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="ask-main">
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
            turn.role === 'system' ? (
              <div key={turn.id} className="ask-turn ask-turn-system">
                <span className="ask-system-marker">{turn.content}</span>
              </div>
            ) : turn.role === 'user' ? (
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

                  {turn.content && turn.streaming && (
                    <div className="ask-answer">{turn.content}</div>
                  )}
                  {!turn.streaming && turn.content && (
                    <CitedAnswer text={turn.content} citations={turn.citations ?? []} />
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
                  {turn.note && (
                    <div className="ask-note">{turn.note}</div>
                  )}
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

                  {turn.citations && turn.citations.length > 0 ? (
                    <CitationList citations={turn.citations} />
                  ) : (
                    turn.sources &&
                    turn.sources.length > 0 && (
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
                    )
                  )}

                  {/* Feedback (FE §6.6) — persisted assistant turns only */}
                  {!turn.streaming &&
                    !turn.errorCode &&
                    turn.content &&
                    !turn.id.startsWith('turn-') && (
                      <div
                        className="ask-feedback"
                        role="group"
                        aria-label="Rate this answer"
                      >
                        <button
                          type="button"
                          className={[
                            'ask-feedback-btn',
                            turn.feedback === 1 ? 'active' : '',
                          ].join(' ')}
                          aria-pressed={turn.feedback === 1}
                          aria-label="Helpful"
                          title="Helpful"
                          onClick={() => handleFeedback(turn.id, 1)}
                        >
                          👍
                        </button>
                        <button
                          type="button"
                          className={[
                            'ask-feedback-btn',
                            turn.feedback === -1 ? 'active' : '',
                          ].join(' ')}
                          aria-pressed={turn.feedback === -1}
                          aria-label="Not helpful"
                          title="Not helpful"
                          onClick={() => handleFeedback(turn.id, -1)}
                        >
                          👎
                        </button>
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
      </section>
    </div>
  )
}
