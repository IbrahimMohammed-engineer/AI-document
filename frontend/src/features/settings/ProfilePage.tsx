/**
 * Profile page (Phase 15 §6.9) — full_name editable; email/org read-only.
 * Save → PATCH /settings/profile → authStore refresh (header name updates).
 */

import { useEffect, useRef, useState } from 'react'

import { PermissionDenied } from '@/components/PermissionDenied'
import { useUpdateProfile } from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import './settings.css'

export function ProfilePage() {
  const currentUser = useAuthStore((s) => s.currentUser)
  const updateProfile = useUpdateProfile()

  const [fullName, setFullName] = useState(currentUser?.full_name ?? '')
  const [status, setStatus] = useState<'idle' | 'saved' | 'error'>('idle')
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (currentUser) setFullName(currentUser.full_name)
  }, [currentUser])

  if (!currentUser) return <PermissionDenied message="Session user unavailable." />

  const dirty = fullName.trim() !== currentUser.full_name

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setStatus('idle')
    updateProfile.mutate(fullName.trim(), {
      onSuccess: () => setStatus('saved'),
      onError: () => setStatus('error'),
    })
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Profile</h1>
          <p className="page-subtitle">Your personal account information</p>
        </div>
      </div>

      <form className="settings-form" onSubmit={handleSubmit} aria-busy={updateProfile.isPending}>
        <div className="settings-field">
          <label htmlFor="profile-full-name">Full name</label>
          <input
            id="profile-full-name"
            ref={inputRef}
            type="text"
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
            minLength={2}
            maxLength={255}
            required
          />
        </div>

        <div className="settings-field">
          <label htmlFor="profile-email">Email</label>
          <input id="profile-email" type="email" value={currentUser.email} readOnly />
          <span className="settings-field-hint">Email cannot be changed.</span>
        </div>

        <div className="settings-field">
          <label htmlFor="profile-org">Organization</label>
          <input id="profile-org" type="text" value={currentUser.organization.name} readOnly />
        </div>

        <div className="settings-actions">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!dirty || updateProfile.isPending}
          >
            {updateProfile.isPending ? 'Saving…' : 'Save changes'}
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

export default ProfilePage

