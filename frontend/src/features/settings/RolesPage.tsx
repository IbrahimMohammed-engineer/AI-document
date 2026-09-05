/**
 * Roles page (Phase 15 §6.9) — read-only view of roles + permission keys.
 * Permission gate: user:manage.
 */

import { useMemo } from 'react'

import { PermissionDenied } from '@/components/PermissionDenied'
import { useRoles } from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import './settings.css'

export function RolesPage() {
  const permissionList = useAuthStore((s) => s.currentUser?.permissions)
  const permissions = useMemo(() => new Set(permissionList ?? []), [permissionList])
  const rolesQuery = useRoles()

  if (!permissions.has('user:manage')) {
    return <PermissionDenied />
  }

  const { data, isLoading, isError, refetch } = rolesQuery

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Roles</h1>
          <p className="page-subtitle">
            What each role can do — read-only reference
          </p>
        </div>
      </div>

      {isLoading ? (
        <div className="card skeleton-card" aria-busy="true">
          <span className="skeleton" style={{ height: '3rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '3rem' }} />
        </div>
      ) : isError ? (
        <div className="card empty-state">
          <div className="empty-state-title">Could not load roles</div>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      ) : (
        (data?.items ?? []).map((role) => (
          <section key={role.id} className="card role-card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <h2 className="section-title" style={{ margin: 0 }}>
                {role.name}
              </h2>
              {role.is_system && <span className="badge badge-gray">System</span>}
            </div>
            <div className="role-permissions">
              {role.permissions.map((permission) => (
                <span key={permission} className="permission-key">
                  {permission}
                </span>
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  )
}

export default RolesPage

