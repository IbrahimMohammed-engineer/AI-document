/**
 * Reset-password page (Phase 2).
 *
 * Reads the single-use reset token from the URL (?token=...), collects the
 * new password twice, and submits POST /auth/reset-password. On success all
 * existing sessions were revoked server-side — the user signs in fresh.
 */
import { useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '@/lib/api/client'
import { resetPasswordApi } from '@/lib/api/auth'
import { AuthCard } from './AuthCard'
import { PasswordInput } from './PasswordInput'

export function ResetPasswordPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = useMemo(() => searchParams.get('token') ?? '', [searchParams])

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [touched, setTouched] = useState({ password: false, confirm: false })
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const passwordError =
    touched.password && password.length < 8 ? 'Password must be at least 8 characters.' : null
  const confirmError =
    touched.confirm && confirm !== password ? 'Passwords do not match.' : null

  const formValid = token.length >= 16 && password.length >= 8 && password === confirm

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await resetPasswordApi({ token, new_password: password })
      navigate('/login?reason=reset', { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        setError('This reset link is invalid, expired, or already used. Request a new one.')
      } else if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (!token) {
    return (
      <AuthCard
        title="Invalid reset link"
        subtitle="The link is missing its token"
        footer={<Link to="/forgot-password">Request a new link</Link>}
      >
        <div className="alert alert-warning">
          Password reset links expire after one hour and work exactly once.
        </div>
      </AuthCard>
    )
  }

  return (
    <AuthCard
      title="Choose a new password"
      subtitle="All active sessions will be signed out after the change"
      footer={<Link to="/login">Back to sign in</Link>}
    >
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-group">
          <label className="form-label" htmlFor="rp-password">New password</label>
          <PasswordInput
            id="rp-password"
            value={password}
            onChange={setPassword}
            onBlur={() => setTouched((t) => ({ ...t, password: true }))}
            autoComplete="new-password"
            invalid={Boolean(passwordError)}
            disabled={submitting}
          />
          {passwordError && <div className="form-error">{passwordError}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="rp-confirm">Confirm new password</label>
          <PasswordInput
            id="rp-confirm"
            value={confirm}
            onChange={setConfirm}
            onBlur={() => setTouched((t) => ({ ...t, confirm: true }))}
            autoComplete="new-password"
            invalid={Boolean(confirmError)}
            disabled={submitting}
          />
          {confirmError && <div className="form-error">{confirmError}</div>}
        </div>

        <button type="submit" className="btn btn-primary btn-block" disabled={!formValid || submitting}>
          {submitting ? <span className="spinner spinner-sm" /> : 'Reset password'}
        </button>
      </form>
    </AuthCard>
  )
}
