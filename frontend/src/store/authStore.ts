/**
 * Auth store (Zustand) — the single source of truth for session state.
 *
 * Security properties (Phase 2):
 *  - accessToken lives in memory ONLY (never localStorage) — lost on reload,
 *    recovered via the httpOnly refresh cookie through refreshAccessToken()
 *  - currentUser mirrors GET /auth/me (including advisory permissions for UI)
 *  - A single in-flight refresh promise dedupes concurrent 401 retries
 */

import { create } from 'zustand'
import { tokenStore } from '@/lib/api/client'
import {
  getMeApi,
  loginApi,
  logoutApi,
  refreshApi,
  type UserMeResponse,
} from '@/lib/api/auth'

interface AuthState {
  accessToken: string | null
  currentUser: UserMeResponse | null
  /** 'initializing' while the boot-time refresh/me probe is running */
  status: 'idle' | 'initializing' | 'ready'

  login: (email: string, password: string, orgSlug: string) => Promise<void>
  logout: () => Promise<void>
  refreshAccessToken: () => Promise<string>
  initialize: () => Promise<void>
  clearSession: () => void
}

// Single-flight refresh: concurrent 401s share one /auth/refresh call
let _refreshPromise: Promise<string> | null = null

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: null,
  currentUser: null,
  status: 'idle',

  login: async (email, password, orgSlug) => {
    const tokens = await loginApi({ email, password, org_slug: orgSlug })
    tokenStore.set(tokens.access_token)
    const currentUser = await getMeApi()
    set({ accessToken: tokens.access_token, currentUser, status: 'ready' })
  },

  logout: async () => {
    try {
      await logoutApi()
    } catch {
      // Best effort — the local session is cleared regardless
    }
    get().clearSession()
  },

  refreshAccessToken: async () => {
    if (_refreshPromise) return _refreshPromise

    _refreshPromise = (async () => {
      const tokens = await refreshApi()
      tokenStore.set(tokens.access_token)
      set({ accessToken: tokens.access_token, status: 'ready' })
      return tokens.access_token
    })()

    try {
      return await _refreshPromise
    } finally {
      _refreshPromise = null
    }
  },

  initialize: async () => {
    set({ status: 'initializing' })
    try {
      const token = await get().refreshAccessToken()
      const currentUser = await getMeApi()
      set({ accessToken: token, currentUser, status: 'ready' })
    } catch {
      get().clearSession()
    }
  },

  clearSession: () => {
    tokenStore.clear()
    set({ accessToken: null, currentUser: null, status: 'ready' })
  },
}))

// ── Selectors ─────────────────────────────────────────────────────────────────

export const selectIsAuthenticated = (state: AuthState): boolean =>
  Boolean(state.accessToken)

export const isAuthenticated = (): boolean =>
  selectIsAuthenticated(useAuthStore.getState())
