/**
 * Users page (Phase 15 §6.9) — member table + role assignment modal.
 * Permission gate: user:manage. Pagination 50/page.
 */

import { useMemo, useState } from 'react'

import { PermissionDenied } from '@/components/PermissionDenied'
import {
  useRoles,
  useSettingsUsers,
  useUpdateUserRoles,
} from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import type { SettingsUserItem } from '@/lib/api/settings'
import './settings.css'

const PAGE_SIZE = 50

function RoleAssignmentModal({
  user,
  onClose,
}: {
  user: SettingsUserItem
  onClose: () => void
}) {
  const rolesQuery = useRoles()
  const updateUserRoles = useUpdateUserRoles()
  const [selectedIds, setSelectedIds] = useState<Set<string>>(
    new Set(user.role_ids),
  )

  function toggle(roleId: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(roleId)) next.delete(roleId)
      else next.add(roleId)
      return next
    })
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    updateUserRoles.mutate(
      { userId: user.id, roleIds: Array.from(selectedIds) },
      { onSuccess: onClose },
    )
  }

  return (
    <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="role-modal-title"
        onSubmit={handleSubmit}
      >
        <h2 id="role-modal-title" className="modal-title">
          Roles for {user.full_name}
        </h2>

        {rolesQuery.isLoading && (
          <span className="spinner spinner-sm" aria-label="Loading roles…" />
        )}

        {(rolesQuery.data?.items ?? []).map((role) => (
          <div key={role.id} className="modal-role-row">
            <input
              id={`role-${role.id}`}
              type="checkbox"
              checked={selectedIds.has(role.id)}
              onChange={() => toggle(role.id)}
            />
            <label htmlFor={`role-${role.id}`}>
              {role.name}
              <span className="settings-field-hint">
                {' '}
                {role.permissions.length} permission{role.permissions.length === 1 ? '' : 's'}
              </span>
            </label>
          </div>
        ))}

        <div className="modal-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary btn-sm"
            disabled={selectedIds.size === 0 || updateUserRoles.isPending}
          >
            {updateUserRoles.isPending ? 'Saving…' : 'Save roles'}
          </button>
        </div>
        {updateUserRoles.isError && (
          <span className="settings-status settings-status--error" role="alert">
            {updateUserRoles.error.message}
          </span>
        )}
      </form>
    </div>
  )
}

export function UsersPage() {
  const permissionList = useAuthStore((s) => s.currentUser?.permissions)
  const permissions = useMemo(() => new Set(permissionList ?? []), [permissionList])
  const [page, setPage] = useState(0)
  const [editing, setEditing] = useState<SettingsUserItem | null>(null)

  const usersQuery = useSettingsUsers(PAGE_SIZE, page * PAGE_SIZE)

  if (!permissions.has('user:manage')) {
    return <PermissionDenied />
  }

  const { data, isLoading, isError, refetch } = usersQuery
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Users</h1>
          <p className="page-subtitle">
            {total} member{total === 1 ? '' : 's'} in your organization
          </p>
        </div>
      </div>

      {isLoading ? (
        <div className="card skeleton-card" aria-busy="true">
          <span className="skeleton" style={{ height: '2rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '2rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '2rem' }} />
        </div>
      ) : isError ? (
        <div className="card empty-state">
          <div className="empty-state-title">Could not load users</div>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      ) : data && data.items.length === 0 ? (
        <div className="card empty-state">
          <div className="empty-state-title">No members yet</div>
        </div>
      ) : (
        <div className="card">
          <table className="settings-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Roles</th>
                <th scope="col">Joined</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((user) => (
                <tr key={user.id}>
                  <td>{user.full_name}</td>
                  <td>{user.email}</td>
                  <td>
                    {user.roles.map((role) => (
                      <span key={role} className="role-badge">
                        {role}
                      </span>
                    ))}
                  </td>
                  <td>{new Date(user.created_at).toLocaleDateString()}</td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => setEditing(user)}
                    >
                      Change roles
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="settings-pagination">
            <span>
              Page {page + 1} of {pageCount}
            </span>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={page + 1 >= pageCount}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
        </div>
      )}

      {editing && <RoleAssignmentModal user={editing} onClose={() => setEditing(null)} />}
    </div>
  )
}

export default UsersPage

