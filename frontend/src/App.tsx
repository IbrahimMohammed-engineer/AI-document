/**
 * Application root — routing and provider setup.
 *
 * Route structure mirrors the Information Architecture from
 * Frontend-Design-Documentation.md §4.2.
 *
 * Phase 2: auth guard (PrivateRoute), public auth routes (login, register,
 * forgot/reset password), boot-time session restore via the refresh cookie,
 * and the session-expired redirect wired to the API client's 401 handler.
 */
import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { setSessionExpiredHandler } from '@/lib/api/client'
import { useAuthStore } from '@/store/authStore'
import {
  ForgotPasswordPage,
  LoginPage,
  RegisterPage,
  ResetPasswordPage,
} from '@/features/auth'
import { DocumentWorkspace } from '@/features/documents'
import { ComparisonPage } from '@/features/documents'
import { AskPage } from '@/features/ask'

// ── Placeholder page components ───────────────────────────────────────────────
// These will be replaced with full implementations in later phases.

function PlaceholderPage({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">{title}</h1>
          <p className="page-subtitle">{description}</p>
        </div>
      </div>
      <div className="card empty-state">
        <div className="empty-state-icon">🚧</div>
        <div className="empty-state-title">Coming in a future phase</div>
        <p className="empty-state-description">
          This page is scaffolded and ready to be implemented. The backend APIs,
          data models, and repositories supporting this feature are being built
          phase by phase.
        </p>
      </div>
    </div>
  )
}

function DashboardPage() {
  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-subtitle">Overview of your organization's document activity</p>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }}>
        {[
          { label: 'Total Documents', value: '—', icon: '📄', phase: 3 },
          { label: 'Active Conversations', value: '—', icon: '✦', phase: 9 },
          { label: 'Documents Processing', value: '—', icon: '⚙', phase: 5 },
          { label: 'Open Conflicts', value: '—', icon: '⚠', phase: 10 },
        ].map((stat) => (
          <div key={stat.label} className="card" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: '1.5rem' }}>{stat.icon}</span>
              <span className="badge badge-gray">Phase {stat.phase}</span>
            </div>
            <div style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--color-neutral-800)' }}>
              {stat.value}
            </div>
            <div className="text-sm text-muted">{stat.label}</div>
          </div>
        ))}
      </div>

      <div className="card">
        <div style={{ marginBottom: '1rem', fontWeight: 600, color: 'var(--color-neutral-700)' }}>
          Implementation Progress
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
          {[
            { phase: 0, label: 'Project Setup & Scaffold', done: true },
            { phase: 1, label: 'Database Foundation & Migrations', done: true },
            { phase: 2, label: 'Authentication & Authorization', done: false },
            { phase: 3, label: 'Document Management', done: false },
            { phase: 4, label: 'Background Processing (Redis/Arq)', done: false },
            { phase: 5, label: 'Document Extraction & OCR', done: false },
            { phase: 6, label: 'Chunking & Structure Detection', done: false },
            { phase: 7, label: 'Embeddings & pgvector', done: false },
            { phase: 8, label: 'Hybrid Search & Reranking', done: false },
            { phase: 9, label: 'RAG Pipeline & Conversations', done: false },
            { phase: 10, label: 'Citations & Conflict Detection', done: false },
          ].map((item) => (
            <div
              key={item.phase}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.75rem',
                padding: '0.5rem 0.75rem',
                borderRadius: 'var(--radius-md)',
                background: item.done ? 'hsl(152,50%,96%)' : 'transparent',
                border: '1px solid',
                borderColor: item.done ? 'hsl(152,40%,85%)' : 'transparent',
              }}
            >
              <span style={{ fontSize: '1rem' }}>{item.done ? '✅' : '⬜'}</span>
              <span style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--color-neutral-600)' }}>
                Phase {item.phase}
              </span>
              <span style={{ fontSize: '0.875rem', color: 'var(--color-neutral-700)' }}>
                {item.label}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Auth guard ────────────────────────────────────────────────────────────────

/**
 * Wraps protected routes. While the boot-time session probe runs it shows a
 * full-page spinner (a valid refresh cookie must not bounce the user to the
 * login screen). Unauthenticated visitors are redirected with a reason code
 * so LoginPage can explain what happened.
 */
function PrivateRoute({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status)
  const authenticated = useAuthStore((s) => Boolean(s.accessToken))
  const location = useLocation()

  if (status === 'initializing') {
    return (
      <div className="auth-page">
        <span className="spinner" aria-label="Loading session…" />
      </div>
    )
  }

  if (!authenticated) {
    return <Navigate to="/login?reason=expired" replace state={{ from: location }} />
  }

  return <>{children}</>
}

// ── Router ────────────────────────────────────────────────────────────────────

export function App() {
  const initialize = useAuthStore((s) => s.initialize)

  // Restore the session once on mount (refresh cookie → access token → /me)
  useEffect(() => {
    void initialize()
  }, [initialize])

  // API client 401 handler — silent refresh already failed at this point
  useEffect(() => {
    setSessionExpiredHandler(() => {
      window.location.assign('/login?reason=expired')
    })
    return () => setSessionExpiredHandler(null)
  }, [])

  return (
    <Routes>
      {/* Redirect root → dashboard (PrivateRoute inside redirects to login) */}
      <Route path="/" element={<Navigate to="/app/dashboard" replace />} />

      {/* Public auth routes */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />

      {/* App routes — auth guarded */}
      <Route
        path="/app"
        element={
          <PrivateRoute>
            <AppShell />
          </PrivateRoute>
        }
      >
        <Route index element={<Navigate to="/app/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="documents" element={<PlaceholderPage title="Documents" description="Document library — Phase 3" />} />
        <Route path="documents/:id" element={<DocumentWorkspace />} />
        <Route path="ask" element={<AskPage />} />
        <Route path="ask/:conversationId" element={<PlaceholderPage title="Conversation" description="AI Chat with citation rendering — Phase 9" />} />
        <Route path="search" element={<PlaceholderPage title="Search" description="Hybrid semantic + keyword search — Phase 8" />} />
        {/* Phase 12 — Document comparison (version picker + live results) */}
        <Route path="compare" element={<ComparisonPage />} />
        <Route path="compare/:comparisonId" element={<ComparisonPage />} />
        <Route path="analytics" element={<PlaceholderPage title="Analytics" description="Usage metrics, processing stats, AI quality — Phase 10" />} />
        <Route path="settings" element={<PlaceholderPage title="Settings" description="Organization, users, roles, integrations — Phase 3+" />} />
        <Route path="settings/*" element={<PlaceholderPage title="Settings" description="Organization settings — Phase 3+" />} />
      </Route>

      {/* 404 */}
      <Route path="*" element={<PlaceholderPage title="Page Not Found" description="The page you're looking for doesn't exist." />} />
    </Routes>
  )
}
