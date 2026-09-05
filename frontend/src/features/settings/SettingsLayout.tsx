/**
 * Settings shell — left nav over the nine sections (Phase 15 §6.9).
 *
 * Members without `user:manage`/`settings:manage` only see Profile; every
 * admin-only route renders <PermissionDenied> for them (the pages gate on
 * the same permission keys the backend enforces). On mobile the nav becomes
 * a horizontal tab bar.
 */

import { useMemo } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import { PermissionDenied } from '@/components/PermissionDenied'
import { useAuthStore } from '@/store/authStore'
import './settings.css'

interface SettingsNavItem {
  to: string
  label: string
  icon: string
  /** Permission key required to open the section (undefined = everyone). */
  permission?: 'user:manage' | 'settings:manage'
}

const settingsNav: SettingsNavItem[] = [
  { to: '/app/settings/profile', label: 'Profile', icon: '👤' },
  { to: '/app/settings/organization', label: 'Organization', icon: '🏢', permission: 'settings:manage' },
  { to: '/app/settings/users', label: 'Users', icon: '👥', permission: 'user:manage' },
  { to: '/app/settings/roles', label: 'Roles', icon: '🔑', permission: 'user:manage' },
  { to: '/app/settings/ai', label: 'AI Settings', icon: '✦', permission: 'settings:manage' },
  { to: '/app/settings/documents', label: 'Documents', icon: '📄', permission: 'settings:manage' },
  { to: '/app/settings/integrations', label: 'Integrations', icon: '⛁', permission: 'settings:manage' },
  { to: '/app/settings/security', label: 'Security', icon: '🔒', permission: 'settings:manage' },
  { to: '/app/settings/audit-log', label: 'Audit Log', icon: '☰', permission: 'settings:manage' },
]

export function SettingsLayout() {
  // Referentially-stable selection: derive the Set in render (a new Set
  // inside the selector would re-render forever via useSyncExternalStore).
  const permissionList = useAuthStore((s) => s.currentUser?.permissions)
  const permissions = useMemo(
    () => new Set(permissionList ?? []),
    [permissionList],
  )
  const visibleNav = settingsNav.filter(
    (item) => !item.permission || permissions.has(item.permission),
  )

  // A member with zero admin permissions never sees admin sections — the
  // Outlet itself is still rendered for Profile (own data, no gate needed).
  const isAdmin = permissions.has('user:manage') || permissions.has('settings:manage')

  return (
    <div className="settings-layout">
      <nav
        className="settings-nav"
        aria-label={isAdmin ? 'Settings sections' : 'Profile settings'}
      >
        {visibleNav.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `settings-nav-item${isActive ? ' active' : ''}`}
          >
            <span className="settings-nav-icon" aria-hidden="true">
              {item.icon}
            </span>
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="settings-content">
        {visibleNav.length === 0 ? (
          <PermissionDenied message="Settings are limited to your profile in this organization." />
        ) : (
          <Outlet />
        )}
      </div>
    </div>
  )
}
