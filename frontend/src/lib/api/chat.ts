/**
 * Chat API client (Phase 11) — persistent, scoped, streaming conversations.
 *
 * Endpoints (Backend §37/§38):
 *   GET    /chat/conversations                — paginated, recency-ordered
 *   POST   /chat/conversations                — lazy creation + first message → SSE
 *   GET    /chat/conversations/:id            — conversation + messages + citations
 *   DELETE /chat/conversations/:id            — archive
 *   POST   /chat/conversations/:id/messages   — SSE stream
 *   POST   /chat/messages/:id/stop            — explicit cancellation
 *   POST   /chat/messages/:id/feedback        — ±1 (upsert)
 *
 * The stream is consumed with the shared SSE transport (lib/realtime) —
 * POST responses cannot use EventSource.
 */

import { del, get, post } from './client'
import { consumeSseStream, isSseOpenError } from '@/lib/realtime'
import type { AskCitation, AskSource } from './ask'

// ─── Types ────────────────────────────────────────────────────────────────────

export type ChatScopeType = 'current_document' | 'selected_documents' | 'knowledge_base'

export interface ChatScopePayload {
  type: ChatScopeType
  document_ids?: string[]
}

export interface ConversationSummary {
  id: string
  title: string | null
  scope_type: ChatScopeType
  message_count: number
  created_at: string
  updated_at: string
}

export interface ConversationListResponse {
  items: ConversationSummary[]
  total: number
  limit: number
  offset: number
}

export type Groundedness = 'grounded' | 'partial' | 'ungrounded'

export interface ChatMessage {
  id: string
  role: 'USER' | 'ASSISTANT' | 'SYSTEM'
  content: string
  created_at: string
  model: string | null
  groundedness: Groundedness | null
  prompt_tokens: number | null
  completion_tokens: number | null
  stopped: boolean
  citations: AskCitation[]
  my_feedback: -1 | 1 | null
}

export interface ConversationDetailResponse {
  conversation: ConversationSummary
  messages: ChatMessage[]
  limit: number
  offset: number
  total_messages: number
}

// ─── SSE events (Backend §37: start → token* → citation* → done) ──────────────

export interface ChatScopeInfo {
  type: ChatScopeType
  document_ids: string[]
}

export type ChatStreamEvent =
  | {
      type: 'start'
      conversation_id: string
      user_message_id: string
      assistant_message_id: string
      scope: ChatScopeInfo
    }
  | { type: 'token'; delta: string }
  | { type: 'citation'; citation: AskCitation }
  /** Phase 13 — deterministic inline conflict notices (§20). */
  | { type: 'conflict_notice'; conflicts: ConflictNotice[] }
  | {
      type: 'done'
      message_id: string | null
      groundedness: Groundedness
      stopped: boolean
      answer: string
      user_message_id: string | null
      citations: AskCitation[]
      sources: AskSource[]
      model: string | null
      prompt_tokens: number
      completion_tokens: number
      stripped_claims: number
      entailment_checks: number
      /** Phase 16 — canary sentinel fired: a document attempted injection. */
      injection_attempt?: boolean
      latency_ms: Record<string, number>
    }
  | { type: 'error'; code: string; message: string }

/** Phase 13 — structured conflict notice (server-computed, never LLM text). */
export interface ConflictNotice {
  conflict_id: string
  topic: string
  severity: 'MAJOR' | 'MODERATE' | 'MINOR'
}

export interface ChatStreamHandlers {
  onEvent: (event: ChatStreamEvent) => void
}

// ─── Conversation reads ───────────────────────────────────────────────────────

export async function listConversationsApi(
  limit = 30,
  offset = 0,
): Promise<ConversationListResponse> {
  return get<ConversationListResponse>(
    `/chat/conversations?limit=${limit}&offset=${offset}`,
  )
}

export async function getConversationApi(
  conversationId: string,
  limit = 200,
  offset = 0,
): Promise<ConversationDetailResponse> {
  return get<ConversationDetailResponse>(
    `/chat/conversations/${conversationId}?limit=${limit}&offset=${offset}`,
  )
}

export async function deleteConversationApi(conversationId: string): Promise<void> {
  await del<void>(`/chat/conversations/${conversationId}`)
}

// ─── Streaming turns ──────────────────────────────────────────────────────────

interface ChatTurnPayload {
  content: string
  scope?: ChatScopePayload
}

/**
 * Send one chat turn and dispatch SSE events.
 *
 * When `conversationId` is omitted (a brand-new conversation) the request
 * goes to POST /chat/conversations — lazy creation (DB §20: empty
 * conversations are never persisted) — and the `start` event carries the
 * new conversation's id.
 */
export async function streamChatMessage(
  conversationId: string | null,
  body: ChatTurnPayload,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const path = conversationId
    ? `/chat/conversations/${conversationId}/messages`
    : '/chat/conversations'
  try {
    await consumeSseStream(path, {
      method: 'POST',
      body,
      signal,
      onEvent: ({ event, data }) => {
        switch (event) {
          case 'start':
            handlers.onEvent({
              type: 'start',
              conversation_id: String(data.conversation_id ?? ''),
              user_message_id: String(data.user_message_id ?? ''),
              assistant_message_id: String(data.assistant_message_id ?? ''),
              scope: (data.scope ?? { type: 'knowledge_base', document_ids: [] }) as ChatScopeInfo,
            })
            break
          case 'token':
            handlers.onEvent({ type: 'token', delta: String(data.delta ?? '') })
            break
          case 'citation':
            handlers.onEvent({ type: 'citation', citation: data as unknown as AskCitation })
            break
          case 'conflict_notice':
            handlers.onEvent({
              type: 'conflict_notice',
              conflicts: (Array.isArray(data.conflicts) ? data.conflicts : []) as ConflictNotice[],
            })
            break
          case 'done':
            handlers.onEvent({ type: 'done', ...(data as object) } as ChatStreamEvent)
            break
          case 'error':
            handlers.onEvent({
              type: 'error',
              code: String(data.code ?? 'UNKNOWN_ERROR'),
              message: String(data.message ?? 'The AI service is unavailable.'),
            })
            break
          default:
            break // forward compatibility
        }
      },
    })
  } catch (err) {
    if (isSseOpenError(err)) {
      throw Object.assign(new Error(err.message), {
        code: err.code,
        httpStatus: err.httpStatus,
      })
    }
    throw err
  }
}

// ─── Stop + feedback ──────────────────────────────────────────────────────────

export async function stopMessageApi(
  messageId: string,
): Promise<{ message_id: string; stopped: boolean }> {
  return post<{ message_id: string; stopped: boolean }>(
    `/chat/messages/${messageId}/stop`,
  )
}

export async function rateMessageApi(
  messageId: string,
  rating: -1 | 1,
  comment?: string | null,
): Promise<{ message_id: string; rating: number; comment: string | null }> {
  return post(`/chat/messages/${messageId}/feedback`, {
    rating,
    comment: comment ?? null,
  })
}
