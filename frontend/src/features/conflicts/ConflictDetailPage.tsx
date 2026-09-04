/**
 * ConflictDetailPage — the /app/conflicts/:id route (Phase 13, FE §6.13).
 *
 * Full statement list with live version-state badges, the section-anchored
 * "Compare Sources" action per adjacent statement pair (reusing Phase 12's
 * comparison workflow — plan §22), and the permission-gated resolution menu.
 */

import { useNavigate, useParams } from 'react-router-dom'

import {
  useConflict,
  useResolveConflict,
} from '@/hooks/queries/useConflicts'
import { useAuthStore } from '@/store/authStore'
import { ConflictSeverityBadge } from './ConflictSeverityBadge'
import { ConflictResolutionMenu } from './ConflictResolutionMenu'
import {
  compareSourcesHref,
  ConflictStatementRow,
} from './ConflictCard'
import './conflicts.css'

export function ConflictDetailPage() {
  const { conflictId } = useParams<{ conflictId: string }>()
  const navigate = useNavigate()
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canResolve = Boolean(permissions?.includes('conflict:resolve'))

  const conflictQuery = useConflict(conflictId)
  const resolveMutation = useResolveConflict(conflictId ?? '')
  const conflict = conflictQuery.data

  if (conflictQuery.isLoading) {
    return (
      <div className="conflicts-page">
        <div className="card conflicts-loading">Loading conflict…</div>
      </div>
    )
  }

  if (conflictQuery.isError || !conflict) {
    return (
      <div className="conflicts-page">
        <div className="card empty-state">
          <div className="empty-state-icon">⚠</div>
          <div className="empty-state-title">Conflict not found</div>
          <p className="empty-state-description">
            It may not exist, or you may not have access to every source
            document involved.
          </p>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => navigate('/app/conflicts')}
          >
            Back to Conflicts
          </button>
        </div>
      </div>
    )
  }

  const statements = conflict.statements
  const pairs: Array<[typeof statements[number], typeof statements[number]]> = []
  for (let i = 0; i + 1 < statements.length; i += 1) {
    for (let j = i + 1; j < statements.length; j += 1) {
      pairs.push([statements[i], statements[j]])
    }
  }
  const showPairActions = statements.length <= 4 // keep the action list bounded
  const likelyResolved = conflict.priority === 'LIKELY_RESOLVED'

  return (
    <div className="conflicts-page">
      <div className="page-header">
        <div>
          <button
            type="button"
            className="conflict-back"
            onClick={() => navigate('/app/conflicts')}
          >
            ← All conflicts
          </button>
          <h1 className="page-title conflict-title">
            <ConflictSeverityBadge severity={conflict.severity} />
            {conflict.topic}
          </h1>
          <p className="page-subtitle">
            Detected {new Date(conflict.detected_at).toLocaleString()} ·{' '}
            {conflict.detection_method === 'BACKGROUND_SCAN'
              ? 'background scan'
              : conflict.detection_method === 'COMPARISON_DERIVED'
                ? 'document comparison'
                : 'retrieval'}
            {likelyResolved && ' · Likely resolved by version update'}
          </p>
        </div>
        {canResolve && conflict.status === 'OPEN' && (
          <ConflictResolutionMenu
            busy={resolveMutation.isPending}
            onResolve={(decision, note) => resolveMutation.mutate({ decision, note })}
          />
        )}
        {conflict.status !== 'OPEN' && (
          <span className="conflict-card__resolved-tag">
            {conflict.status}
            {conflict.resolved_at
              ? ` · ${new Date(conflict.resolved_at).toLocaleDateString()}`
              : ''}
          </span>
        )}
      </div>

      {likelyResolved && (
        <div className="conflicts-banner conflicts-banner--muted" role="status">
          <span className="conflicts-banner__icon">✓</span>
          <span>
            At least one statement's source version is no longer current — a
            newer version may already resolve this conflict.
          </span>
        </div>
      )}

      {conflict.resolution_note && (
        <div className="card conflict-resolution-note">
          <div className="conflict-resolution-note__label">Resolution note</div>
          {conflict.resolution_note}
        </div>
      )}

      <div className="conflict-detail-statements">
        {statements.map((statement) => (
          <ConflictStatementRow
            key={statement.id}
            statement={statement}
            compareHref={
              statements.length >= 2
                ? `/app/compare?${(() => {
                    const other = statements.find((s) => s.id !== statement.id)
                    return other
                      ? new URLSearchParams({
                          documentA: statement.document_id,
                          documentB: other.document_id,
                          versionA: statement.document_version_id,
                          versionB: other.document_version_id,
                          ...(statement.section
                            ? { section: statement.section }
                            : {}),
                        }).toString()
                      : ''
                })()}`
                : undefined
            }
          />
        ))}
      </div>

      {showPairActions && pairs.length > 0 && (
        <div className="card conflict-pairs">
          <div className="conflict-pairs__title">Compare statement pairs</div>
          {pairs.map(([a, b]) => (
            <button
              key={`${a.id}-${b.id}`}
              type="button"
              className="conflict-pairs__row"
              onClick={() =>
                navigate(compareSourcesHref(a, b, a.section ?? b.section))
              }
            >
              Compare “{a.document_name}” ↔ “{b.document_name}”
              {a.section ? ` — ${a.section}` : ''}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
