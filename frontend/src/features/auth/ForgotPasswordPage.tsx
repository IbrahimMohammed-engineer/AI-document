/**
 * Forgot-password page (Phase 2).
 *
 * Requests a reset token. The API response is deliberately opaque (same 200
 * whether or not the account exists) — this page always shows the same
 * confirmation banner. In development the response also carries the raw token
 * for easy testing; production sends a real email (Phase 16).
 */
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { forgotPasswordApi } from '@/lib/api/auth'
import { AuthCard } from './AuthCard'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function ForgotPasswordPage() {
  const [orgSlug, setOrgSlug] = useState('')
  const [email, setEmail] = useState('')
  const [touched, setTouched] = useState({ orgSlug: false, email: false })
  const [submitted, setSubmitted] = useState(false)
  const [devToken, setDevToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const orgSlugError = touched.orgSlug && orgSlug.trim().length < 2 ? 'Enter your organization slug.' : null
  const emailError = touched.email && !EMAIL_RE.test(email) ? 'Enter a valid email address.' : null
  const formValid = orgSlug.trim().length >= 2 && EMAIL_RE.test(email)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const result = await forgotPasswordApi({ email, org_slug: orgSlug.trim() })
      setDevToken(result.reset_token)
      setSubmitted(true)
    } catch {
      setError('Something went wrong requesting the reset. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  if (submitted) {
    return (
      <AuthCard
        title="Check your inbox"
        subtitle="If an account exists for that email, a reset link is on its way"
        footer={<Link to="/login">Back to sign in</Link>}
      >
        <div className="alert alert-info">
          The link is valid for one hour and can be used only once.
        </div>
        {devToken && (
          <div className="alert alert-warning">
            <div className="font-medium">Development mode</div>
            <div className="text-sm" style={{ marginTop: '0.25rem' }}>
              Email delivery isn't configured yet. Reset now:{' '}
              <Link to={`/reset-password?token=${encodeURIComponent(devToken)}`}>
                open reset form
              </Link>
            </div>
          </div>
        )}
      </AuthCard>
    )
  }

  return (
    <AuthCard
      title="Reset your password"
      subtitle="Enter your organization and account email"
      footer={<Link to="/login">Back to sign in</Link>}
    >
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-group">
          <label className="form-label" htmlFor="fp-org">Organization</label>
          <input
            id="fp-org"
            className={`input ${orgSlugError ? 'input-invalid' : ''}`}
            type="text"
            value={orgSlug}
            onChange={(e) => setOrgSlug(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, orgSlug: true }))}
            placeholder="your-company"
            disabled={submitting}
          />
          {orgSlugError && <div className="form-error">{orgSlugError}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="fp-email">Email</label>
          <input
            id="fp-email"
            className={`input ${emailError ? 'input-invalid' : ''}`}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, email: true }))}
            placeholder="you@company.com"
            autoComplete="username"
            disabled={submitting}
          />
          {emailError && <div className="form-error">{emailError}</div>}
        </div>

        <button type="submit" className="btn btn-primary btn-block" disabled={!formValid || submitting}>
          {submitting ? <span className="spinner spinner-sm" /> : 'Send reset link'}
        </button>
      </form>
    </AuthCard>
  )
}
