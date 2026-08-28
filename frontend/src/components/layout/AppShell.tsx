/**
 * App Shell — persistent sidebar + header layout.
 *
 * Renders the dark sidebar with navigation items and the top header bar.
 * The main content area is a slot for the current route's page component.
 *
 * Phase 2: header shows the signed-in user (from the auth store) with a
 * sign-out action. Nav items will be permission-filtered in later phases.
 */
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'
import { ProcessingIndicator } from '@/features/documents'

interface NavItem {
  to: string
  label: string
  icon: string
  end?: boolean
}

const mainNavItems: NavItem[] = [
  { to: '/app/dashboard', label: 'Dashboard', icon: '⊞', end: true },
  { to: '/app/documents', label: 'Documents', icon: '📄' },
  { to: '/app/ask', label: 'Ask AI', icon: '✦' },
  { to: '/app/search', label: 'Search', icon: '⌕' },
  { to: '/app/compare', label: 'Compare', icon: '⊟' },
]

const analyticsNavItems: NavItem[] = [
  { to: '/app/analytics', label: 'Analytics', icon: '◎' },
]

const settingsNavItems: NavItem[] = [
  { to: '/app/settings', label: 'Settings', icon: '⚙' },
]

export function AppShell() {
  const navigate = useNavigate()
  const currentUser = useAuthStore((s) => s.currentUser)
  const logout = useAuthStore((s) => s.logout)

  async function handleLogout() {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-shell">
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className="app-sidebar">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">✦</div>
          <div>
            <div className="sidebar-logo-text">DocIntelligence</div>
            <div className="sidebar-logo-subtitle">Enterprise AI Platform</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <span className="sidebar-section-label">Workspace</span>
          {mainNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="nav-item-icon">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}

          <span className="sidebar-section-label">Insights</span>
          {analyticsNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="nav-item-icon">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}

          <span className="sidebar-section-label">Organization</span>
          {settingsNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="nav-item-icon">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* ── Main area ────────────────────────────────────────────────── */}
      <div className="app-main">
        <header className="app-header">
          <div style={{ flex: 1 }} />
          {/* Phase 4 — org-wide active processing jobs (FE §5.2) */}
          {currentUser && <ProcessingIndicator />}
          <div className="header-user">
            {currentUser && (
              <>
                <div className="header-user-meta">
                  <div className="header-user-name">{currentUser.full_name}</div>
                  <div className="header-user-org">{currentUser.organization.name}</div>
                </div>
                <div className="header-user-avatar" aria-hidden="true">
                  {currentUser.full_name.slice(0, 1).toUpperCase()}
                </div>
              </>
            )}
            <button type="button" className="btn btn-secondary btn-sm" onClick={handleLogout}>
              Sign out
            </button>
          </div>
        </header>

        <main className="app-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
