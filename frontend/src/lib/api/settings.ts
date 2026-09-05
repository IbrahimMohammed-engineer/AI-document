/**
 * Typed API client for Settings + audit-log reads (Phase 15).
 *
 * Matches backend/app/api/settings.py:
 *   PATCH /settings/profile                  → updateProfileApi()
 *   PATCH /settings/organization             → updateOrganizationApi()
 *   GET   /settings/users                    → listSettingsUsersApi()
 *   PATCH /settings/users/{user_id}/roles    → updateUserRolesApi()
 *   GET   /settings/roles                    → listRolesApi()
 *   GET   /audit-logs                        → listAuditLogsApi()
 */

import { get, patch } from './client'
import type { OrganizationResponse, UserMeResponse } from './auth'

// ─── Types ────────────────────────────────────────────────────────────────────

export interface SettingsUserItem {
  id: string
  email: string
  full_name: string
  roles: string[]
  role_ids: string[]
  is_active: boolean
  created_at: string
}

export interface SettingsUsersResponse {
  items: SettingsUserItem[]
  total: number
  limit: number
  offset: number
}

export interface RoleItem {
  id: string
  name: string
  is_system: boolean
  permissions: string[]
}

export interface RolesResponse {
  items: RoleItem[]
  total: number
}

export interface AuditLogItem {
  id: string
  organization_id: string
  user_id: string | null
  user_email: string | null
  action: string
  resource_type: string
  resource_id: string | null
  metadata: Record<string, unknown>
  ip_address: string | null
  created_at: string
}

export interface AuditLogsResponse {
  items: AuditLogItem[]
  total: number
  limit: number
  offset: number
}

export interface AuditLogFilters {
  action?: string
  resource_type?: string
  user_id?: string
  from?: string
  to?: string
  limit?: number
  offset?: number
}

// ─── Endpoints ────────────────────────────────────────────────────────────────

/** Update the caller's own display name — returns the refreshed /me shape. */
export function updateProfileApi(full_name: string): Promise<UserMeResponse> {
  return patch<UserMeResponse>('/settings/profile', { full_name })
}

/** Update the organization display name (slug is immutable). */
export function updateOrganizationApi(name: string): Promise<OrganizationResponse> {
  return patch<OrganizationResponse>('/settings/organization', { name })
}

/** Paginated member list (user:manage). */
export function listSettingsUsersApi(
  params: { limit?: number; offset?: number } = {},
): Promise<SettingsUsersResponse> {
  const query = new URLSearchParams()
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<SettingsUsersResponse>(`/settings/users${suffix}`)
}

/** Full-replacement role update for one member (user:manage). */
export function updateUserRolesApi(
  userId: string,
  roleIds: string[],
): Promise<SettingsUserItem> {
  return patch<SettingsUserItem>(`/settings/users/${userId}/roles`, {
    role_ids: roleIds,
  })
}

/** Assignable roles with permission keys (user:manage). */
export function listRolesApi(): Promise<RolesResponse> {
  return get<RolesResponse>('/settings/roles')
}

/** Filtered, newest-first audit-log page (settings:manage). */
export function listAuditLogsApi(
  filters: AuditLogFilters = {},
): Promise<AuditLogsResponse> {
  const query = new URLSearchParams()
  if (filters.action) query.set('action', filters.action)
  if (filters.resource_type) query.set('resource_type', filters.resource_type)
  if (filters.user_id) query.set('user_id', filters.user_id)
  if (filters.from) query.set('from', filters.from)
  if (filters.to) query.set('to', filters.to)
  if (filters.limit != null) query.set('limit', String(filters.limit))
  if (filters.offset != null) query.set('offset', String(filters.offset))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return get<AuditLogsResponse>(`/audit-logs${suffix}`)
}
