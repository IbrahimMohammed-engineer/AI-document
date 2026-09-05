/**
 * React Query hooks for document permission grants (Phase 16).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  grantDocumentPermission,
  listDocumentPermissions,
  revokeDocumentPermission,
  type GrantCreateRequest,
  type PermissionListResponse,
} from '@/lib/api/permissions'
import { useAuthStore } from '@/store/authStore'

export const permissionKeys = {
  all: (documentId: string) => ['documents', documentId, 'permissions'] as const,
}

export function useDocumentPermissions(documentId: string | null) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<PermissionListResponse>({
    queryKey: permissionKeys.all(documentId ?? 'none'),
    enabled: Boolean(documentId) && Boolean(accessToken),
    queryFn: () => listDocumentPermissions(documentId as string),
    staleTime: 15_000,
  })
}

export function useGrantPermission(documentId: string) {
  const queryClient = useQueryClient()
  return useMutation<
    Awaited<ReturnType<typeof grantDocumentPermission>>,
    Error,
    GrantCreateRequest
  >({
    mutationFn: (body) => grantDocumentPermission(documentId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: permissionKeys.all(documentId),
      })
    },
  })
}

export function useRevokePermission(documentId: string) {
  const queryClient = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (userId) => revokeDocumentPermission(documentId, userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: permissionKeys.all(documentId),
      })
    },
  })
}
