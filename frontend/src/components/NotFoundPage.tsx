/**
 * 404 page (Phase 15 §6.1.5) — replaces the last PlaceholderPage.
 */

import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="card empty-state">
      <div className="empty-state-icon" aria-hidden="true">
        ∅
      </div>
      <div className="empty-state-title">Page not found</div>
      <p className="empty-state-description">
        The page you're looking for doesn't exist or has been moved.
      </p>
      <Link to="/app/dashboard" className="btn btn-primary btn-sm">
        Go to Dashboard
      </Link>
    </div>
  )
}
