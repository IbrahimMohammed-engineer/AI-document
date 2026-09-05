/**
 * Shared permission-denied state (Phase 15 §6.11).
 *
 * The single reusable "you can't see this" surface. Every screen shows this
 * instead of a generic error card when an API call returns 403 — the state
 * matrix (FE §18) treats permission-denied as a distinct, named state.
 */

import { Link } from 'react-router-dom'

export function PermissionDenied({ message }: { message?: string }) {
  return (
    <div className="card empty-state" role="alert" aria-live="polite">
      <div className="empty-state-icon" aria-hidden="true">
        🔒
      </div>
      <div className="empty-state-title">Access denied</div>
      <p className="empty-state-description">
        {message ??
          "You don't have permission to view this. Ask an organization admin if you need access."}
      </p>
      <Link to="/app/dashboard" className="btn btn-secondary btn-sm">
        Back to Dashboard
      </Link>
    </div>
  )
}
