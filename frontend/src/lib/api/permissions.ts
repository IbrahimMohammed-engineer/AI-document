/**
 * Document permission API (Phase 16 — RESTRICTED grant management).
 *
 * Explicit per-user access grants on documents. The document owner and
 * users with the `document:admin` permission may manage grants; everyone
 * else is rejected by the backend (403 same-org / 404 cross-org).
 */

import { del, get, post } from '@/lib/api/client'

export interface PermissionGrant {
  id: string
  user_id: string
  grantee_email: string | null
  grantee_full_name: string | null
  permission_type: 'read' | 'write' | 'admin'
  expires_at: string | null
  created_at: string
}

export interface PermissionListResponse {
  document_id: string
  items: PermissionGrant[]
}

export interface GrantCreateRequest {
  user_id: string
  permission_type?: 'read' | 'write' | 'admin'
  expires_at?: string | null
}

/** List explicit grants on a document (owner or document:admin). */
export async function listDocumentPermissions(
  documentId: string,
): Promise<PermissionListResponse> {
  return get<PermissionListResponse>(`/documents/${documentId}/permissions`)
}

/** Grant a user explicit access (409 if a grant already exists). */
export async function grantDocumentPermission(
  documentId: string,
  body: GrantCreateRequest,
): Promise<PermissionGrant> {
  return post<PermissionGrant>(`/documents/${documentId}/permissions`, body)
}

/** Revoke a user's explicit grant. */
export async function revokeDocumentPermission(
  documentId: string,
  userId: string,
): Promise<void> {
  await del<void>(`/documents/${documentId}/permissions/${userId}`)
}
