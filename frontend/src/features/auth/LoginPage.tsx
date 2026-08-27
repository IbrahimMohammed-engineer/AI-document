/**
 * Login page (Phase 2).
 *
 * Security-conscious UX:
 *  - Generic error banner (backend returns identical 401s for wrong password,
 *    unknown email, and unknown org — nothing to enumerate here either)
 *  - Rate-limit countdown after a 429
 *  - SSO placeholder for future org IdP integration
 */
import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '@/lib/api/client'
import { useAuthStore } from '@/store/authStore'
import { AuthCard } from './AuthCard'
import { PasswordInput } from './PasswordInput'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const REASON_BANNERS: Record<string, string> = {
  expired: 'Your session has expired. Please sign in again.',
  reset: 'Password updated. Sign in with your new password.',
}

export function LoginPage() {
  const navigate = useNavigate()
  const login = useAuthStore((s) => s.login)
  const [searchParams] = useSearchParams()
  const reasonBanner = REASON_BANNERS[searchParams.get('reason') ?? ''] ?? null

  const [orgSlug, setOrgSlug] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [touched, setTouched] = useState({ orgSlug: false, email: false, password: false })
  const [error, setError] = useState<string | null>(null)
  const [rateLimitSeconds, setRateLimitSeconds] = useState(0)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (rateLimitSeconds <= 0) return
    const timer = setInterval(() => setRateLimitSeconds((s) => s - 1), 1000)
    return () => clearInterval(timer)
  }, [rateLimitSeconds])

  const orgSlugError = touched.orgSlug && orgSlug.trim().length < 2 ? 'Enter your organization slug.' : null
  const emailError = touched.email && !EMAIL_RE.test(email) ? 'Enter a valid email address.' : null
  const passwordError = touched.password && password.length < 1 ? 'Enter your password.' : null

  const formValid = orgSlug.trim().length >= 2 && EMAIL_RE.test(email) && password.length >= 1

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password, orgSlug.trim())
      navigate('/app/dashboard', { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.isRateLimited) {
        setRateLimitSeconds(err.retryAfterSeconds ?? 60)
      } else if (err instanceof ApiError && err.isUnauthorized) {
        setError('Invalid credentials. Check your email, password, and organization.')
      } else if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const blocked = rateLimitSeconds > 0

  return (
    <AuthCard
      title="Sign in"
      subtitle="Access your organization's document workspace"
      footer={
        <>
          Don't have an account? <Link to="/register">Create one</Link>
        </>
      }
    >
      {reasonBanner && (
        <div className="alert alert-info" role="status">
          {reasonBanner}
        </div>
      )}
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}
      {blocked && (
        <div className="alert alert-warning" role="alert">
          Too many attempts. Try again in {rateLimitSeconds}s.
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-group">
          <label className="form-label" htmlFor="login-org">Organization</label>
          <input
            id="login-org"
            className={`input ${orgSlugError ? 'input-invalid' : ''}`}
            type="text"
            value={orgSlug}
            onChange={(e) => setOrgSlug(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, orgSlug: true }))}
            placeholder="your-company"
            autoComplete="off"
            disabled={blocked || submitting}
          />
          {orgSlugError && <div className="form-error">{orgSlugError}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="login-email">Email</label>
          <input
            id="login-email"
            className={`input ${emailError ? 'input-invalid' : ''}`}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, email: true }))}
            placeholder="you@company.com"
            autoComplete="username"
            disabled={blocked || submitting}
          />
          {emailError && <div className="form-error">{emailError}</div>}
        </div>

        <div className="form-group">
          <div className="form-label-row">
            <label className="form-label" htmlFor="login-password">Password</label>
            <Link to="/forgot-password" className="text-xs">Forgot password?</Link>
          </div>
          <PasswordInput
            id="login-password"
            value={password}
            onChange={setPassword}
            onBlur={() => setTouched((t) => ({ ...t, password: true }))}
            invalid={Boolean(passwordError)}
            disabled={blocked || submitting}
          />
          {passwordError && <div className="form-error">{passwordError}</div>}
        </div>

        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={!formValid || blocked || submitting}
        >
          {submitting ? <span className="spinner spinner-sm" /> : 'Sign in'}
        </button>
      </form>

      <div className="auth-divider"><span>or</span></div>

      <button type="button" className="btn btn-secondary btn-block" disabled title="Coming soon">
        Continue with SSO
      </button>
    </AuthCard>
  )
}
