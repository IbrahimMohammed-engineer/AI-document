/**
 * React Query hooks for Settings + audit logs (Phase 15).
 *
 * Query keys: ['settings', …]. Audit logs keep a 60s staleTime (§6.14
 * staleTime discipline). Profile/org mutations refresh the auth store's
 * currentUser via authStore.initialize() (§6.14 invalidation map).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  listAuditLogsApi,
  listRolesApi,
  listSettingsUsersApi,
  updateOrganizationApi,
  updateProfileApi,
  updateUserRolesApi,
  type AuditLogFilters,
} from '@/lib/api/settings'
import { useAuthStore } from '@/store/authStore'

export const settingsKeys = {
  users: (limit: number, offset: number) => ['settings', 'users', limit, offset] as const,
  roles: () => ['settings', 'roles'] as const,
  auditLogs: (filters: AuditLogFilters) => ['audit-logs', filters] as const,
}

// ─── Users ────────────────────────────────────────────────────────────────────

export function useSettingsUsers(limit = 50, offset = 0) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery({
    queryKey: settingsKeys.users(limit, offset),
    enabled: Boolean(accessToken),
    queryFn: () => listSettingsUsersApi({ limit, offset }),
    staleTime: 30_000,
  })
}

export function useUpdateUserRoles() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, roleIds }: { userId: string; roleIds: string[] }) =>
      updateUserRolesApi(userId, roleIds),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings', 'users'] })
    },
  })
}

// ─── Roles ────────────────────────────────────────────────────────────────────

export function useRoles() {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery({
    queryKey: settingsKeys.roles(),
    enabled: Boolean(accessToken),
    queryFn: listRolesApi,
    staleTime: 5 * 60_000,
  })
}

// ─── Profile / organization ───────────────────────────────────────────────────

export function useUpdateProfile() {
  const initialize = useAuthStore((s) => s.initialize)
  return useMutation({
    mutationFn: (fullName: string) => updateProfileApi(fullName),
    onSuccess: async (me) => {
      // Re-fetch /me through the store so the header name updates
      useAuthStore.setState({ currentUser: me })
      await initialize()
    },
  })
}

export function useUpdateOrganization() {
  const initialize = useAuthStore((s) => s.initialize)
  return useMutation({
    mutationFn: (name: string) => updateOrganizationApi(name),
    onSuccess: async () => {
      // The org name is embedded in /auth/me — refresh it
      await initialize()
    },
  })
}

// ─── Audit logs ───────────────────────────────────────────────────────────────

export function useAuditLogs(filters: AuditLogFilters = {}) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery({
    queryKey: settingsKeys.auditLogs(filters),
    enabled: Boolean(accessToken),
    queryFn: () => listAuditLogsApi(filters),
    staleTime: 60_000,
  })
}
