/**
 * Organization settings (Phase 15 §6.9) — org name editable, slug immutable.
 * Permission gate: settings:manage (mirrors the backend requirement).
 */

import { useEffect, useMemo, useState } from 'react'

import { PermissionDenied } from '@/components/PermissionDenied'
import { useUpdateOrganization } from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import './settings.css'

export function OrganizationPage() {
  const currentUser = useAuthStore((s) => s.currentUser)
  const permissionList = useAuthStore((s) => s.currentUser?.permissions)
  const permissions = useMemo(() => new Set(permissionList ?? []), [permissionList])
  const updateOrganization = useUpdateOrganization()

  const [name, setName] = useState(currentUser?.organization.name ?? '')
  const [status, setStatus] = useState<'idle' | 'saved' | 'error'>('idle')

  useEffect(() => {
    if (currentUser) setName(currentUser.organization.name)
  }, [currentUser])

  if (!permissions.has('settings:manage')) {
    return <PermissionDenied />
  }
  if (!currentUser) return null

  const dirty = name.trim() !== currentUser.organization.name

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setStatus('idle')
    updateOrganization.mutate(name.trim(), {
      onSuccess: () => setStatus('saved'),
      onError: () => setStatus('error'),
    })
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Organization</h1>
          <p className="page-subtitle">Your organization's identity</p>
        </div>
      </div>

      <form className="settings-form" onSubmit={handleSubmit} aria-busy={updateOrganization.isPending}>
        <div className="settings-field">
          <label htmlFor="org-name">Organization name</label>
          <input
            id="org-name"
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            minLength={2}
            maxLength={255}
            required
          />
        </div>

        <div className="settings-field">
          <label htmlFor="org-slug">Slug</label>
          <input id="org-slug" type="text" value={currentUser.organization.slug} readOnly />
          <span className="settings-field-hint">
            The slug is permanent — it appears in login URLs.
          </span>
        </div>

        <div className="settings-actions">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!dirty || updateOrganization.isPending}
          >
            {updateOrganization.isPending ? 'Saving…' : 'Save changes'}
          </button>
          {status === 'saved' && (
            <span className="settings-status settings-status--ok" role="status">
              Saved
            </span>
          )}
          {status === 'error' && (
            <span className="settings-status settings-status--error" role="alert">
              Could not save — try again
            </span>
          )}
        </div>
      </form>
    </div>
  )
}

export default OrganizationPage

