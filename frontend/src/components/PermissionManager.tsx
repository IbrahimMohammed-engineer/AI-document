/**
 * PermissionManager — "Manage Access" modal for RESTRICTED documents
 * (Phase 16, plan §14.2).
 *
 * Visible to the document owner or users holding `document:admin` (the
 * caller decides; the backend enforces regardless). Lists current grants
 * and allows granting/revoking explicit access by user UUID.
 */
import { useState } from 'react'

import {
  useDocumentPermissions,
  useGrantPermission,
  useRevokePermission,
} from '@/hooks/queries/usePermissions'

interface PermissionManagerProps {
  documentId: string
  documentName: string
  open: boolean
  onClose: () => void
}

export function PermissionManager({
  documentId,
  documentName,
  open,
  onClose,
}: PermissionManagerProps) {
  const permissionsQuery = useDocumentPermissions(open ? documentId : null)
  const grant = useGrantPermission(documentId)
  const revoke = useRevokePermission(documentId)

  const [granteeId, setGranteeId] = useState('')
  const [permissionType, setPermissionType] = useState<'read' | 'write' | 'admin'>('read')
  const [error, setError] = useState<string | null>(null)

  if (!open) return null

  const grants = permissionsQuery.data?.items ?? []

  function handleGrant() {
    setError(null)
    const userId = granteeId.trim()
    if (!userId) {
      setError('Enter the user UUID to grant.')
      return
    }
    grant.mutate(
      { user_id: userId, permission_type: permissionType },
      {
        onSuccess: () => setGranteeId(''),
        onError: (e) => setError(e.message),
      },
    )
  }

  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="perm-mgr-title">
        <h2 id="perm-mgr-title" className="modal-title">
          Manage Access — {documentName}
        </h2>
        <p className="text-sm text-muted">
          Restricted documents are hidden from AI answers and search unless a
          user holds an explicit grant.
        </p>

        {permissionsQuery.isLoading ? (
          <p className="text-sm text-muted">Loading grants…</p>
        ) : grants.length === 0 ? (
          <p className="text-sm text-muted">No explicit grants yet.</p>
        ) : (
          <table className="settings-table">
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Access</th>
                <th scope="col">Expires</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {grants.map((g) => (
                <tr key={g.id}>
                  <td>{g.grantee_email ?? g.user_id}</td>
                  <td>{g.permission_type}</td>
                  <td>{g.expires_at ? new Date(g.expires_at).toLocaleString() : 'Never'}</td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={revoke.isPending}
                      onClick={() => revoke.mutate(g.user_id)}
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
          <input
            className="input"
            style={{ flex: 1 }}
            placeholder="Grantee user UUID"
            value={granteeId}
            onChange={(e) => setGranteeId(e.target.value)}
            aria-label="Grantee user UUID"
          />
          <select
            className="input"
            value={permissionType}
            onChange={(e) =>
              setPermissionType(e.target.value as 'read' | 'write' | 'admin')
            }
            aria-label="Permission type"
          >
            <option value="read">read</option>
            <option value="write">write</option>
            <option value="admin">admin</option>
          </select>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={handleGrant}
            disabled={grant.isPending}
          >
            {grant.isPending ? 'Granting…' : 'Grant'}
          </button>
        </div>
        {error && (
          <p className="text-sm" style={{ color: 'var(--color-error)' }} role="alert">
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
