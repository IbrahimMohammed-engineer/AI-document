/**
 * SSE transport library (Phase 11 — FE §11 Real-Time Processing UX).
 *
 * Both streaming surfaces — chat (`POST /chat/...` returns the stream
 * directly) and document processing (`GET /documents/.../stream`) — are
 * consumed with fetch + ReadableStream, because `EventSource` cannot send
 * the Authorization header.  This module owns the wire format; lifecycle
 * policies (reconnect, REST refetch, polling fallback) live with the
 * callers (FE §11.3 — the stream is an optimization, REST is the truth).
 *
 * Wire format handled here:
 *   event: <name>\n
 *   data: <json>\n\n          → dispatched as { event, data }
 *   : keep-alive\n\n          → heartbeat comment, silently ignored
 */

import { API_BASE_URL, tokenStore } from '@/lib/api/client'

export interface SseEvent {
  event: string
  data: Record<string, unknown>
}

export interface ConsumeSseOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  signal?: AbortSignal
  onEvent: (event: SseEvent) => void
}

/**
 * Thrown when the stream cannot be opened (non-200 before any event).
 * Shaped like ApiError so callers can branch on code/httpStatus uniformly.
 */
export class SseOpenError extends Error {
  readonly code: string
  readonly httpStatus: number

  constructor(code: string, message: string, httpStatus: number) {
    super(message)
    this.name = 'SseOpenError'
    this.code = code
    this.httpStatus = httpStatus
  }

  get isUnauthorized() { return this.httpStatus === 401 }
  get isForbidden() { return this.httpStatus === 403 }
  get isNotFound() { return this.httpStatus === 404 }
}

/** True when this error means "the transport itself failed" (retryable). */
export function isSseOpenError(err: unknown): err is SseOpenError {
  return err instanceof SseOpenError
}

/**
 * Open an SSE endpoint and dispatch parsed events until the stream ends,
 * the signal aborts, or an open-error is thrown.
 *
 * AbortSignal aborts surface as normal return (callers use abort for the
 * stop control); genuine transport failures throw TypeError, which callers
 * treat as a reconnect trigger.
 */
export async function consumeSseStream(
  url: string,
  options: ConsumeSseOptions,
): Promise<void> {
  const token = tokenStore.get()
  const method = options.method ?? 'GET'
  const response = await fetch(url.startsWith('http') ? url : `${API_BASE_URL}${url}`, {
    method,
    headers: {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
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
    throw new SseOpenError(code, message, response.status)
  }

  if (!response.body) {
    throw new SseOpenError('STREAM_UNSUPPORTED', 'Streaming is not supported by this browser.', 0)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (block: string) => {
    const trimmed = block.trim()
    if (!trimmed) return
    let eventName = ''
    const dataLines: string[] = []
    for (const line of trimmed.split('\n')) {
      if (line.startsWith(':')) continue // heartbeat / comment
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    if (!eventName) return
    let data: Record<string, unknown> = {}
    try {
      data = dataLines.length ? JSON.parse(dataLines.join('\n')) : {}
    } catch {
      return // malformed payload — skip the block rather than killing the stream
    }
    options.onEvent({ event: eventName, data })
  }

  try {
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
  } catch (err) {
    if ((err as Error).name === 'AbortError') return
    throw err
  }
}
