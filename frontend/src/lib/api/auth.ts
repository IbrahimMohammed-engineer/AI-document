/**
 * Typed API functions for the auth endpoints (Phase 2).
 *
 * Matches backend/app/schemas/auth.py exactly.
 */

import { get, post } from './client'

// ── Response / request types ──────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string
  expires_in: number
  token_type: string
}

export interface OrganizationResponse {
  id: string
  name: string
  slug: string
}

export interface UserMeResponse {
  id: string
  email: string
  full_name: string
  organization: OrganizationResponse
  permissions: string[]
}

export interface RegisterRequest {
  org_name: string
  slug: string
  email: string
  full_name: string
  password: string
}

export interface LoginRequest {
  email: string
  password: string
  org_slug: string
}

export interface ForgotPasswordRequest {
  email: string
  org_slug: string
}

export interface ForgotPasswordResponse {
  message: string
  /** Present in development mode only — the email provider lands in Phase 16. */
  reset_token: string | null
}

export interface ResetPasswordRequest {
  token: string
  new_password: string
}

// ── API functions ─────────────────────────────────────────────────────────────

export function registerApi(payload: RegisterRequest): Promise<TokenResponse> {
  return post<TokenResponse>('/auth/register', payload)
}

export function loginApi(payload: LoginRequest): Promise<TokenResponse> {
  return post<TokenResponse>('/auth/login', payload)
}

export function refreshApi(): Promise<TokenResponse> {
  return post<TokenResponse>('/auth/refresh')
}

export function logoutApi(): Promise<void> {
  return post<void>('/auth/logout')
}

export function getMeApi(): Promise<UserMeResponse> {
  return get<UserMeResponse>('/auth/me')
}

export function forgotPasswordApi(
  payload: ForgotPasswordRequest
): Promise<ForgotPasswordResponse> {
  return post<ForgotPasswordResponse>('/auth/forgot-password', payload)
}

export function resetPasswordApi(payload: ResetPasswordRequest): Promise<void> {
  return post<void>('/auth/reset-password', payload)
}
