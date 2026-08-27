/**
 * Registration page (Phase 2) — org creation + first Admin user.
 *
 * Creates a new organization and the bootstrap admin account in one step
 * (POST /auth/register). Invite-based user addition arrives in Phase 3.
 */
import { useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '@/lib/api/client'
import { registerApi } from '@/lib/api/auth'
import { tokenStore } from '@/lib/api/client'
import { getMeApi } from '@/lib/api/auth'
import { useAuthStore } from '@/store/authStore'
import { AuthCard } from './AuthCard'
import { PasswordInput } from './PasswordInput'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60)
}

function passwordStrength(pw: string): 0 | 1 | 2 | 3 | 4 {
  if (pw.length < 8) return 0
  let score = 1
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score += 1
  if (/\d/.test(pw)) score += 1
  if (/[^A-Za-z0-9]/.test(pw)) score += 1
  return Math.min(score, 4) as 1 | 2 | 3 | 4
}

const STRENGTH_LABELS = ['', 'Weak', 'Fair', 'Good', 'Strong'] as const

export function RegisterPage() {
  const navigate = useNavigate()

  const [orgName, setOrgName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugEdited, setSlugEdited] = useState(false)
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [acceptToS, setAcceptToS] = useState(false)
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Slug auto-derives from the org name until manually edited
  const effectiveSlug = slugEdited ? slug : slugify(orgName)

  const strength = useMemo(() => passwordStrength(password), [password])

  const errors = {
    orgName: touched.orgName && orgName.trim().length < 2 ? 'Organization name is too short.' : null,
    slug:
      touched.slug && effectiveSlug.length < 2
        ? 'Slug must be at least 2 characters.'
        : null,
    fullName: touched.fullName && fullName.trim().length < 2 ? 'Enter your full name.' : null,
    email: touched.email && !EMAIL_RE.test(email) ? 'Enter a valid email address.' : null,
    password:
      touched.password && password.length < 8
        ? 'Password must be at least 8 characters.'
        : null,
    acceptToS: touched.acceptToS && !acceptToS ? 'You must accept the terms to continue.' : null,
  }

  const formValid =
    orgName.trim().length >= 2 &&
    effectiveSlug.length >= 2 &&
    fullName.trim().length >= 2 &&
    EMAIL_RE.test(email) &&
    password.length >= 8 &&
    acceptToS

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const tokens = await registerApi({
        org_name: orgName.trim(),
        slug: effectiveSlug,
        email,
        full_name: fullName.trim(),
        password,
      })
      tokenStore.set(tokens.access_token)
      const currentUser = await getMeApi()
      useAuthStore.setState({ accessToken: tokens.access_token, currentUser, status: 'ready' })
      navigate('/app/dashboard', { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFLICT') {
        setError('That organization slug is already taken. Pick another one.')
        setSlugEdited(true)
      } else if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const mark = (field: string) => () => setTouched((t) => ({ ...t, [field]: true }))

  return (
    <AuthCard
      title="Create your workspace"
      subtitle="Register your organization and become its first admin"
      footer={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-group">
          <label className="form-label" htmlFor="reg-org-name">Organization name</label>
          <input
            id="reg-org-name"
            className={`input ${errors.orgName ? 'input-invalid' : ''}`}
            type="text"
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
            onBlur={mark('orgName')}
            placeholder="Acme Corporation"
            disabled={submitting}
          />
          {errors.orgName && <div className="form-error">{errors.orgName}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="reg-slug">Workspace slug</label>
          <input
            id="reg-slug"
            className={`input ${errors.slug ? 'input-invalid' : ''}`}
            type="text"
            value={effectiveSlug}
            onChange={(e) => {
              setSlugEdited(true)
              setSlug(slugify(e.target.value))
            }}
            onBlur={mark('slug')}
            placeholder="acme-corporation"
            disabled={submitting}
          />
          {errors.slug && <div className="form-error">{errors.slug}</div>}
          <div className="form-hint">
            Your team signs in with this slug: <code>{effectiveSlug || 'your-company'}</code>
          </div>
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="reg-name">Full name</label>
          <input
            id="reg-name"
            className={`input ${errors.fullName ? 'input-invalid' : ''}`}
            type="text"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            onBlur={mark('fullName')}
            placeholder="Ada Lovelace"
            autoComplete="name"
            disabled={submitting}
          />
          {errors.fullName && <div className="form-error">{errors.fullName}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="reg-email">Work email</label>
          <input
            id="reg-email"
            className={`input ${errors.email ? 'input-invalid' : ''}`}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={mark('email')}
            placeholder="you@company.com"
            autoComplete="username"
            disabled={submitting}
          />
          {errors.email && <div className="form-error">{errors.email}</div>}
        </div>

        <div className="form-group">
          <label className="form-label" htmlFor="reg-password">Password</label>
          <PasswordInput
            id="reg-password"
            value={password}
            onChange={setPassword}
            onBlur={mark('password')}
            autoComplete="new-password"
            invalid={Boolean(errors.password)}
            disabled={submitting}
          />
          {errors.password && <div className="form-error">{errors.password}</div>}
          {password && (
            <div className="strength-meter" aria-label={`Password strength: ${STRENGTH_LABELS[strength]}`}>
              <div className="strength-segments">
                {[1, 2, 3, 4].map((level) => (
                  <span
                    key={level}
                    className={`strength-segment strength-${level <= strength ? STRENGTH_LABELS[strength].toLowerCase() : 'empty'}`}
                  />
                ))}
              </div>
              <span className="strength-label text-xs">{STRENGTH_LABELS[strength] || 'Too short'}</span>
            </div>
          )}
        </div>

        <div className="form-group">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={acceptToS}
              onChange={(e) => setAcceptToS(e.target.checked)}
              onBlur={mark('acceptToS')}
              disabled={submitting}
            />
            <span className="text-sm">
              I accept the <a href="#terms" onClick={(e) => e.preventDefault()}>Terms of Service</a> and{' '}
              <a href="#privacy" onClick={(e) => e.preventDefault()}>Privacy Policy</a>.
            </span>
          </label>
          {errors.acceptToS && <div className="form-error">{errors.acceptToS}</div>}
        </div>

        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={!formValid || submitting}
        >
          {submitting ? <span className="spinner spinner-sm" /> : 'Create workspace'}
        </button>
      </form>
    </AuthCard>
  )
}
