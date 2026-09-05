/**
 * Ask AI API — SSE streaming client (Phases 9–10).
 *
 * POST /ask returns a text/event-stream: `token`* → `sources` → `done`
 * (plus `error` for provider failures).  Axios buffers responses, so the
 * stream is consumed with fetch + ReadableStream; the Authorization header
 * comes from the same in-memory token store the axios client uses.
 *
 * Insufficient evidence is a SUCCESS-shaped stream (sources: [], done with
 * groundedness: 'ungrounded') — the FE §6.6 "no grounded answer found"
 * state, never an error banner.
 *
 * Phase 10: the done payload now carries resolved `citations[]` (FE §12
 * Citation contract) and the post-validation `answer` text — citations are
 * emitted only AFTER generation + validation complete, never mid-stream.
 */

import { API_BASE_URL } from './client'
import { tokenStore } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

/** One source attached to the answer (the context blocks behind it). */
export interface AskSource {
  index: number
  chunk_id: string
  document_id: string
  document_version_id: string
  document_name: string
  page_id: string
  page_number: number
  section_title: string | null
  relevance: number
  snippet: string
}

/**
 * One resolved, validated citation (FE §12 Citation contract): the numbered
 * marker matching the inline [N] in the answer, the exact quoted span with
 * char offsets (REAL source text — never model paraphrase), its surrounding
 * context, and the document/version/page navigation target.
 */
export interface AskCitation {
  index: number
  chunk_id: string
  document_id: string
  document_version_id: string
  document_name: string
  version_number: number | null
  effective_date: string | null
  page_id: string
  page: number
  section: string | null
  text: string
  char_start: number | null
  char_end: number | null
  context_before: string
  context_after: string
  relevance: number
}

export interface AskScope {
  document_ids?: string[]
  collection_ids?: string[]
}

export interface AskHistoryTurn {
  role: 'user' | 'assistant'
  content: string
}

export interface AskRequestBody {
  question: string
  scope?: AskScope
  history?: AskHistoryTurn[]
}

export type AskStreamEvent =
  | { type: 'token'; text: string }
  | { type: 'sources'; sources: AskSource[] }
  | {
      type: 'done'
      groundedness: 'grounded' | 'partial' | 'ungrounded'
      message: string | null
      message_id: string | null
      /** Post-validation answer text — render THIS, not the raw token stream. */
      answer: string
      citations: AskCitation[]
      model: string | null
      prompt_tokens: number
      completion_tokens: number
      intent: string
      topic: string | null
      used_rewrite: boolean
      used_reranker: boolean
      regenerated: boolean
      stripped_claims: number
      entailment_checks: number
      latency_ms: Record<string, number>
    }
  | { type: 'error'; code: string; message: string }

export interface AskStreamHandlers {
  onEvent: (event: AskStreamEvent) => void
}

// ─── SSE parsing ──────────────────────────────────────────────────────────────

/**
 * Parse one SSE block (`event: X\ndata: {...}`) into a typed AskStreamEvent.
 * Unknown event types are ignored (forward compatibility).
 */
function parseSseBlock(block: string): AskStreamEvent | null {
  let eventName = ''
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!eventName) return null
  const data = dataLines.length ? JSON.parse(dataLines.join('\n')) : {}
  switch (eventName) {
    case 'token':
      return { type: 'token', text: String(data.text ?? '') }
    case 'sources':
      return { type: 'sources', sources: data.sources ?? [] }
    case 'done':
      return { type: 'done', ...data }
    case 'error':
      return {
        type: 'error',
        code: String(data.code ?? 'UNKNOWN_ERROR'),
        message: String(data.message ?? 'The AI service is unavailable.'),
      }
    default:
      return null
  }
}

// ─── Stream request ───────────────────────────────────────────────────────────

/** Build the request scope from the ScopeSelector state (empty selection → all). */
export function toAskScope(
  mode: 'all' | 'selected',
  selectedIds: string[],
): AskScope | undefined {
  if (mode === 'selected' && selectedIds.length > 0) {
    return { document_ids: selectedIds }
  }
  return undefined
}

/**
 * POST /ask and dispatch each parsed SSE event to `onEvent`.
 *
 * The caller controls cancellation via `signal` (the FE §6.6 stop-generating
 * control; server-side cancellation lands in Phase 11).  Resolves when the
 * stream ends; throws ApiError-shaped errors only for non-200 responses
 * before the stream starts (401/422 etc.).
 */
export async function streamAsk(
  body: AskRequestBody,
  handlers: AskStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = tokenStore.get()
  const response = await fetch(`${API_BASE_URL}/ask`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok) {
    let code = 'UNKNOWN_ERROR'
    let message = 'An unexpected error occurred.'
    try {
      const payload = await response.json()
      if (payload?.error) {
        code = payload.error.code ?? code
        message = payload.error.message ?? message
      }
    } catch {
      // non-JSON error body — keep the defaults
    }
    throw Object.assign(new Error(message), { code, httpStatus: response.status })
  }

  if (!response.body) {
    throw Object.assign(new Error('Streaming is not supported by this browser.'), {
      code: 'STREAM_UNSUPPORTED',
    })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (block: string) => {
    const trimmed = block.trim()
    if (!trimmed) return
    try {
      const event = parseSseBlock(trimmed)
      if (event) handlers.onEvent(event)
    } catch {
      // Malformed block — skip it rather than killing the stream
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let separator = buffer.indexOf('\n\n')
    while (separator !== -1) {
      dispatch(buffer.slice(0, separator))
      buffer = buffer.slice(separator + 2)
      separator = buffer.indexOf('\n\n')
    }
  }
  // Flush any final unterminated block (defensive — the server always ends with \n\n)
  dispatch(buffer)
}
