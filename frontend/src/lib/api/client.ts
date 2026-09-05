/**
 * Typed API client for the AI Document Intelligence Platform backend.
 *
 * All HTTP requests go through this client. It:
 *  - Attaches the Authorization header from the in-memory token store
 *  - Parses the standard error envelope { error: { code, message, field, requestId } }
 *  - Throws ApiError instances so React Query's error boundary gets typed errors
 *  - Forwards X-Request-Id from responses for correlation
 *  - On 401: silently refreshes the access token once and retries the request;
 *    if the refresh fails, clears the session and fires the expired handler
 *    (App.tsx wires it to redirect /login?reason=expired)
 */

import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'

// ── Error types ───────────────────────────────────────────────────────────────

export interface ApiErrorPayload {
  code: string
  message: string
  field?: string
  requestId?: string
}

export class ApiError extends Error {
  readonly code: string
  readonly field?: string
  readonly requestId?: string
  readonly httpStatus: number
  /** Parsed Retry-After header (seconds) — set on 429 responses */
  readonly retryAfterSeconds: number | null

  constructor(payload: ApiErrorPayload, httpStatus: number, retryAfterSeconds: number | null = null) {
    super(payload.message)
    this.name = 'ApiError'
    this.code = payload.code
    this.field = payload.field
    this.requestId = payload.requestId
    this.httpStatus = httpStatus
    this.retryAfterSeconds = retryAfterSeconds
  }

  get isUnauthorized() { return this.httpStatus === 401 }
  get isForbidden() { return this.httpStatus === 403 }
  get isNotFound() { return this.httpStatus === 404 }
  get isValidation() { return this.httpStatus === 422 }
  get isRateLimited() { return this.httpStatus === 429 }
}

// ── Token store (in-memory, not localStorage) ─────────────────────────────────
// Access tokens are short-lived (15 min) and stored in memory only.
// Refresh tokens are managed via httpOnly cookies (Phase 2).

let _accessToken: string | null = null

export const tokenStore = {
  get: () => _accessToken,
  set: (token: string | null) => { _accessToken = token },
  clear: () => { _accessToken = null },
}

// ── Axios instance ────────────────────────────────────────────────────────────

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

/** Raw API origin — used by non-axios transports (SSE via fetch, Phase 9). */
export const API_BASE_URL = BASE_URL

const axiosInstance: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30_000,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true, // send httpOnly refresh token cookie
})

// ── Request interceptor — attach Authorization ─────────────────────────────

axiosInstance.interceptors.request.use((config) => {
  const token = tokenStore.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ── Response interceptors — refresh-and-retry on 401, parse error envelopes ─

// Set once the router is available (App.tsx) — keeps the client router-agnostic
let _onSessionExpired: (() => void) | null = null

export function setSessionExpiredHandler(handler: (() => void) | null): void {
  _onSessionExpired = handler
}

// Request marker: this request is already a retry after a token refresh —
// never refresh twice for the same original call.
const _RETRY_FLAG = '__authRetry__'

axiosInstance.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error) => {
    if (error.response) {
      const { status, config, data } = error.response
      const payload: ApiErrorPayload = data?.error ?? {
        code: 'UNKNOWN_ERROR',
        message: 'An unexpected error occurred.',
      }
      const apiError = new ApiError(
        payload,
        status,
        Number(error.response.headers?.['retry-after']) || null
      )

      // 401 on a data route → one silent token refresh, then a single retry.
      // The /auth/refresh call itself must NOT be retried (it IS the refresh).
      const isRefreshCall = Boolean(config?.url?.includes('/auth/refresh'))
      if (status === 401 && !isRefreshCall && config && !config[_RETRY_FLAG]) {
        // Lazy import keeps the client ↔ store module cycle runtime-safe
        const { useAuthStore } = await import('@/store/authStore')
        try {
          const token = await useAuthStore.getState().refreshAccessToken()
          config[_RETRY_FLAG] = true
          config.headers = config.headers ?? {}
          config.headers.Authorization = `Bearer ${token}`
          return axiosInstance.request(config)
        } catch {
          useAuthStore.getState().clearSession()
          _onSessionExpired?.()
        }
      }

      throw apiError
    }
    // Network error / timeout
    throw new ApiError(
      {
        code: 'NETWORK_ERROR',
        message: 'Could not connect to the server. Please check your connection.',
      },
      0
    )
  }
)

// ── Typed request helpers ─────────────────────────────────────────────────────

export async function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await axiosInstance.get<T>(url, config)
  return response.data
}

export async function post<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const response = await axiosInstance.post<T>(url, data, config)
  return response.data
}

export async function put<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const response = await axiosInstance.put<T>(url, data, config)
  return response.data
}

export async function patch<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  const response = await axiosInstance.patch<T>(url, data, config)
  return response.data
}

export async function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await axiosInstance.delete<T>(url, config)
  return response.data
}

export default axiosInstance
