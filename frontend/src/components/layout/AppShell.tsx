/**
 * App Shell — persistent sidebar + header layout (Phase 15 §6.1).
 *
 * Phase 15 additions over the Phase 2 skeleton:
 *  - Collapsible sidebar (persisted in uiStore; `[` shortcut; mobile drawer)
 *  - UserMenu dropdown (Profile → Settings → Sign out)
 *  - Command-palette trigger button + ⌘K/Ctrl+K global listener
 *  - Breadcrumbs rendered from route handles
 *  - Suspense boundary around <Outlet /> for route-level code splitting
 *  - Global keyboard shortcuts (G D / G A / G S navigation chords)
 */

import { Suspense, useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'

import { Breadcrumbs } from '@/components/layout/Breadcrumbs'
import { CommandPalette } from '@/components/CommandPalette'
// Direct import (not the feature barrel) — the barrel re-exports PdfViewer,
// which would pull pdfjs-dist into the eager bundle (§6.14).
import { ProcessingIndicator } from '@/features/documents/ProcessingIndicator'
import { useAuthStore } from '@/store/authStore'
import { useUiStore } from '@/store/uiStore'

interface NavItem {
  to: string
  label: string
  icon: string
  end?: boolean
}

const mainNavItems: NavItem[] = [
  { to: '/app/dashboard', label: 'Dashboard', icon: '⊞', end: true },
  { to: '/app/documents', label: 'Documents', icon: '📄' },
  { to: '/app/research', label: 'Research', icon: '⛁' },
  { to: '/app/ask', label: 'Ask AI', icon: '✦' },
  { to: '/app/search', label: 'Search', icon: '⌕' },
  { to: '/app/compare', label: 'Compare', icon: '⊟' },
  { to: '/app/conflicts', label: 'Conflicts', icon: '⚠' },
]

const analyticsNavItems: NavItem[] = [
  { to: '/app/analytics', label: 'Analytics', icon: '◎' },
]

const settingsNavItems: NavItem[] = [
  { to: '/app/settings', label: 'Settings', icon: '⚙' },
]

/** True when a keyboard shortcut chord should be ignored (typing context). */
function isTypingContext(): boolean {
  const el = document.activeElement
  if (!el) return false
  const tag = el.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    el.getAttribute('contenteditable') === 'true'
  )
}

export function AppShell() {
  const navigate = useNavigate()
  const currentUser = useAuthStore((s) => s.currentUser)
  const logout = useAuthStore((s) => s.logout)

  const sidebarCollapsed = useUiStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUiStore((s) => s.toggleSidebar)
  const openCommandPalette = useUiStore((s) => s.openCommandPalette)

  // Mobile slide-out drawer (below 768px the sidebar is hidden entirely)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement | null>(null)

  async function handleLogout() {
    setUserMenuOpen(false)
    await logout()
    navigate('/login', { replace: true })
  }

  // ── Global keyboard shortcuts (§6.13) ─────────────────────────────────────
  useEffect(() => {
    let lastGAt = 0

    const onKeyDown = (event: KeyboardEvent) => {
      // ⌘K / Ctrl+K works everywhere (even while typing)
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        useUiStore.getState().openCommandPalette()
        return
      }
      if (isTypingContext() || event.metaKey || event.ctrlKey || event.altKey) return

      const key = event.key
      if (key === '[') {
        event.preventDefault()
        toggleSidebar()
        return
      }

      // G-chords: G then D/A/S within 1s
      const now = Date.now()
      if (key.toLowerCase() === 'g') {
        lastGAt = now
        return
      }
      if (now - lastGAt <= 1000) {
        const chord = key.toLowerCase()
        if (chord === 'd') navigate('/app/dashboard')
        else if (chord === 'a') navigate('/app/ask')
        else if (chord === 's') navigate('/app/search')
        lastGAt = 0
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [navigate, toggleSidebar])

  // Close the user menu on outside click / Escape
  useEffect(() => {
    if (!userMenuOpen) return
    const onClick = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setUserMenuOpen(false)
      }
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setUserMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [userMenuOpen])

  const sidebarClasses = [
    'app-sidebar',
    sidebarCollapsed ? 'app-sidebar--collapsed' : '',
    mobileNavOpen ? 'app-sidebar--mobile-open' : '',
  ]
    .filter(Boolean)
    .join(' ')

  const renderNavItems = (items: NavItem[]) =>
    items.map((item) => (
      <NavLink
        key={item.to}
        to={item.to}
        end={item.end}
        className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
        title={item.label}
        onClick={() => setMobileNavOpen(false)}
      >
        <span className="nav-item-icon" aria-hidden="true">
          {item.icon}
        </span>
        <span className="nav-item-label">{item.label}</span>
      </NavLink>
    ))

  return (
    <div className="app-shell">
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className={sidebarClasses}>
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon" aria-hidden="true">
            ✦
          </div>
          <div className="nav-item-label">
            <div className="sidebar-logo-text">DocIntelligence</div>
            <div className="sidebar-logo-subtitle">Enterprise AI Platform</div>
          </div>
        </div>

        <nav className="sidebar-nav" aria-label="Main navigation">
          <span className="sidebar-section-label nav-item-label">Workspace</span>
          {renderNavItems(mainNavItems)}

          <span className="sidebar-section-label nav-item-label">Insights</span>
          {renderNavItems(analyticsNavItems)}

          <span className="sidebar-section-label nav-item-label">Organization</span>
          {renderNavItems(settingsNavItems)}
        </nav>

        <button
          type="button"
          className="sidebar-collapse-toggle"
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Toggle sidebar'}
          aria-expanded={!sidebarCollapsed}
        >
          <span aria-hidden="true">{sidebarCollapsed ? '»' : '«'}</span>
          <span className="nav-item-label">Collapse</span>
        </button>
      </aside>

      {mobileNavOpen && (
        <div
          className="sidebar-mobile-backdrop"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* ── Main area ────────────────────────────────────────────────── */}
      <div className="app-main">
        <header className="app-header">
          <button
            type="button"
            className="header-hamburger"
            aria-label="Open navigation menu"
            onClick={() => setMobileNavOpen(true)}
          >
            ☰
          </button>

          <div style={{ flex: 1, minWidth: 0 }}>
            <Breadcrumbs />
          </div>

          <button
            type="button"
            className="command-palette-button"
            onClick={openCommandPalette}
            aria-label="Open command palette"
            title="Command palette (Ctrl+K)"
          >
            <span aria-hidden="true">⌕</span>
            <kbd aria-hidden="true">⌘K</kbd>
          </button>

          {/* Phase 4 — org-wide active processing jobs (FE §5.2) */}
          {currentUser && <ProcessingIndicator />}

          <div className="header-user" ref={userMenuRef}>
            {currentUser && (
              <button
                type="button"
                className="header-user-trigger"
                aria-haspopup="menu"
                aria-expanded={userMenuOpen}
                onClick={() => setUserMenuOpen((open) => !open)}
              >
                <div className="header-user-meta">
                  <div className="header-user-name">{currentUser.full_name}</div>
                  <div className="header-user-org">{currentUser.organization.name}</div>
                </div>
                <div className="header-user-avatar" aria-hidden="true">
                  {currentUser.full_name.slice(0, 1).toUpperCase()}
                </div>
              </button>
            )}

            {userMenuOpen && currentUser && (
              <div className="user-menu" role="menu" aria-label="User menu">
                <Link
                  role="menuitem"
                  to="/app/settings/profile"
                  className="user-menu-item"
                  onClick={() => setUserMenuOpen(false)}
                >
                  Profile
                </Link>
                <Link
                  role="menuitem"
                  to="/app/settings"
                  className="user-menu-item"
                  onClick={() => setUserMenuOpen(false)}
                >
                  Settings
                </Link>
                <button
                  type="button"
                  role="menuitem"
                  className="user-menu-item user-menu-item--danger"
                  onClick={handleLogout}
                >
                  Sign out
                </button>
              </div>
            )}
          </div>
        </header>

        <main className="app-content">
          <Suspense fallback={<div className="page-loading">Loading…</div>}>
            <Outlet />
          </Suspense>
        </main>
      </div>

      <CommandPalette />
    </div>
  )
}
