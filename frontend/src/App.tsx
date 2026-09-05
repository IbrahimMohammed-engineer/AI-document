/**
 * Application root — routing and provider setup.
 *
 * Phase 15: all page components are React.lazy()-loaded (route-level code
 * splitting, §6.1.4); the PlaceholderPage components are gone — Documents,
 * Search, Research, Settings and the 404 route are real screens now. Each
 * route declares `handle: { crumb }` for the Breadcrumbs component.
 */
import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { NotFoundPage } from '@/components/NotFoundPage'
import { setSessionExpiredHandler } from '@/lib/api/client'
import { useAuthStore } from '@/store/authStore'

// ── Route-level code splitting (§6.1.4 / §6.14) ──────────────────────────────
import { lazy, Suspense } from 'react'

const LoginPage = lazy(() =>
  import('@/features/auth').then((m) => ({ default: m.LoginPage })),
)
const RegisterPage = lazy(() =>
  import('@/features/auth').then((m) => ({ default: m.RegisterPage })),
)
const ForgotPasswordPage = lazy(() =>
  import('@/features/auth').then((m) => ({ default: m.ForgotPasswordPage })),
)
const ResetPasswordPage = lazy(() =>
  import('@/features/auth').then((m) => ({ default: m.ResetPasswordPage })),
)

const DashboardPage = lazy(() => import('@/features/dashboard/DashboardPage'))
const DocumentsPage = lazy(() => import('@/features/documents/DocumentsPage'))
const DocumentWorkspace = lazy(() =>
  import('@/features/documents').then((m) => ({ default: m.DocumentWorkspace })),
)
const ComparisonPage = lazy(() =>
  import('@/features/documents').then((m) => ({ default: m.ComparisonPage })),
)
const AskPage = lazy(() =>
  import('@/features/ask').then((m) => ({ default: m.AskPage })),
)
const ResearchWorkspace = lazy(() =>
  import('@/features/research/ResearchWorkspace').then((m) => ({
    default: m.ResearchWorkspace,
  })),
)
const SearchPage = lazy(() => import('@/features/search/SearchPage'))
const ConflictsPage = lazy(() =>
  import('@/features/conflicts').then((m) => ({ default: m.ConflictsPage })),
)
const ConflictDetailPage = lazy(() =>
  import('@/features/conflicts').then((m) => ({ default: m.ConflictDetailPage })),
)
const SummaryPage = lazy(() =>
  import('@/features/summary').then((m) => ({ default: m.SummaryPage })),
)
const ExtractionListPage = lazy(() =>
  import('@/features/extraction').then((m) => ({ default: m.ExtractionListPage })),
)
const ExtractionDetailPage = lazy(() =>
  import('@/features/extraction').then((m) => ({ default: m.ExtractionDetailPage })),
)
const AnalyticsPage = lazy(() =>
  import('@/features/analytics').then((m) => ({ default: m.AnalyticsPage })),
)

const SettingsLayout = lazy(() =>
  import('@/features/settings/SettingsLayout').then((m) => ({
    default: m.SettingsLayout,
  })),
)
const ProfilePage = lazy(() => import('@/features/settings/ProfilePage'))
const OrganizationPage = lazy(() => import('@/features/settings/OrganizationPage'))
const UsersPage = lazy(() => import('@/features/settings/UsersPage'))
const RolesPage = lazy(() => import('@/features/settings/RolesPage'))
const AISettingsPage = lazy(() => import('@/features/settings/AISettingsPage'))
const DocumentSettingsPage = lazy(() =>
  import('@/features/settings/DocumentSettingsPage'),
)
const IntegrationsPage = lazy(() => import('@/features/settings/IntegrationsPage'))
const SecurityPage = lazy(() => import('@/features/settings/SecurityPage'))
const AuditLogPage = lazy(() => import('@/features/settings/AuditLogPage'))

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
      <Route
        path="/login"
        element={
          <Suspense fallback={null}>
            <LoginPage />
          </Suspense>
        }
      />
      <Route
        path="/register"
        element={
          <Suspense fallback={null}>
            <RegisterPage />
          </Suspense>
        }
      />
      <Route
        path="/forgot-password"
        element={
          <Suspense fallback={null}>
            <ForgotPasswordPage />
          </Suspense>
        }
      />
      <Route
        path="/reset-password"
        element={
          <Suspense fallback={null}>
            <ResetPasswordPage />
          </Suspense>
        }
      />

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
        <Route
          path="dashboard"
          element={<DashboardPage />}
          handle={{ crumb: 'Dashboard' }}
        />
        {/* Phase 15 — Documents library (replaces PlaceholderPage) */}
        <Route
          path="documents"
          element={<DocumentsPage />}
          handle={{ crumb: 'Documents' }}
        />
        <Route
          path="documents/:id"
          element={<DocumentWorkspace />}
          handle={{ crumb: (params) => params.id ?? 'Document' }}
        />
        <Route
          path="documents/:id/summary"
          element={<SummaryPage />}
          handle={{ crumb: 'Summary' }}
        />
        <Route
          path="documents/:id/extractions"
          element={<ExtractionListPage />}
          handle={{ crumb: 'Extractions' }}
        />
        <Route
          path="documents/:id/extractions/:extractionId"
          element={<ExtractionDetailPage />}
          handle={{ crumb: 'Extraction' }}
        />
        {/* Phase 15 — Research Workspace (three-panel) */}
        <Route
          path="research"
          element={<ResearchWorkspace />}
          handle={{ crumb: 'Research' }}
        />
        <Route path="ask" element={<AskPage />} handle={{ crumb: 'Ask AI' }} />
        {/* Phase 15 — Search page (replaces PlaceholderPage) */}
        <Route path="search" element={<SearchPage />} handle={{ crumb: 'Search' }} />
        {/* Phase 12 — Document comparison (version picker + live results) */}
        <Route
          path="compare"
          element={<ComparisonPage />}
          handle={{ crumb: 'Compare' }}
        />
        <Route
          path="compare/:comparisonId"
          element={<ComparisonPage />}
          handle={{ crumb: 'Comparison' }}
        />
        {/* Phase 13 — Conflict detection (list + review/resolution workflow) */}
        <Route
          path="conflicts"
          element={<ConflictsPage />}
          handle={{ crumb: 'Conflicts' }}
        />
        <Route
          path="conflicts/:id"
          element={<ConflictDetailPage />}
          handle={{ crumb: 'Conflict' }}
        />
        <Route
          path="analytics"
          element={<AnalyticsPage />}
          handle={{ crumb: 'Analytics' }}
        />
        {/* Phase 15 — full Settings tree (replaces PlaceholderPage) */}
        <Route path="settings" element={<SettingsLayout />} handle={{ crumb: 'Settings' }}>
          <Route index element={<Navigate to="profile" replace />} />
          <Route path="profile" element={<ProfilePage />} handle={{ crumb: 'Profile' }} />
          <Route
            path="organization"
            element={<OrganizationPage />}
            handle={{ crumb: 'Organization' }}
          />
          <Route path="users" element={<UsersPage />} handle={{ crumb: 'Users' }} />
          <Route path="roles" element={<RolesPage />} handle={{ crumb: 'Roles' }} />
          <Route
            path="ai"
            element={<AISettingsPage />}
            handle={{ crumb: 'AI Settings' }}
          />
          <Route
            path="documents"
            element={<DocumentSettingsPage />}
            handle={{ crumb: 'Documents' }}
          />
          <Route
            path="integrations"
            element={<IntegrationsPage />}
            handle={{ crumb: 'Integrations' }}
          />
          <Route
            path="security"
            element={<SecurityPage />}
            handle={{ crumb: 'Security' }}
          />
          <Route
            path="audit-log"
            element={<AuditLogPage />}
            handle={{ crumb: 'Audit Log' }}
          />
        </Route>
      </Route>

      {/* 404 */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
