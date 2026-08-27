/**
 * Shared shell for all auth pages (login / register / forgot / reset).
 *
 * Centered card on the app background with logo, title, and subtitle —
 * per Frontend-Design-Documentation.md §6 (auth screens).
 */
import type { ReactNode } from 'react'

interface AuthCardProps {
  title: string
  subtitle?: string
  children: ReactNode
  footer?: ReactNode
}

export function AuthCard({ title, subtitle, children, footer }: AuthCardProps) {
  return (
    <div className="auth-page">
      <div className="auth-card card">
        <div className="auth-brand">
          <div className="auth-brand-icon">✦</div>
          <div>
            <div className="auth-brand-name">AI Document Intelligence</div>
            <div className="auth-brand-tagline">Grounded answers, cited evidence</div>
          </div>
        </div>

        <h1 className="auth-title">{title}</h1>
        {subtitle && <p className="auth-subtitle">{subtitle}</p>}

        {children}

        {footer && <div className="auth-footer">{footer}</div>}
      </div>
    </div>
  )
}
