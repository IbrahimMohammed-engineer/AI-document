/**
 * ConflictCard — one conflict in the list / detail views (Phase 13, FE §6.13).
 *
 * Per the mockup: severity badge, topic, per-statement document name +
 * effective date (+ superseded de-emphasis), citation-style source display,
 * and the "Compare Sources" / "Resolve ▾" actions (resolution is
 * permission-gated — hidden for users without conflict:resolve).
 */

import { useNavigate } from 'react-router-dom'

import type { ConflictStatement, ConflictSummary } from '@/lib/api/conflicts'
import { useResolveConflict } from '@/hooks/queries/useConflicts'
import { ConflictSeverityBadge } from './ConflictSeverityBadge'
import { ConflictResolutionMenu } from './ConflictResolutionMenu'

export function ConflictStatementRow({
  statement,
  compareHref,
}: {
  statement: ConflictStatement
  compareHref?: string
}) {
  const superseded = statement.version_state !== 'CURRENT'
  return (
    <div className={`conflict-statement${superseded ? ' conflict-statement--superseded' : ''}`}>
      <div className="conflict-statement__source">
        <span className="conflict-statement__doc">{statement.document_name}</span>
        <span className="conflict-statement__meta">
          v{statement.version_number}
          {statement.effective_date ? ` · effective ${statement.effective_date}` : ''}
          {statement.section ? ` · ${statement.section}` : ''}
        </span>
        {superseded && (
          <span
            className="conflict-statement__state-badge"
            title="A newer version of this document exists — the conflict is likely resolved."
          >
            {statement.version_state === 'SCHEDULED' ? 'Scheduled' : 'Superseded'}
          </span>
        )}
      </div>
      <blockquote className="conflict-statement__text">“{statement.statement_text}”</blockquote>
      {compareHref && (
        <button
          type="button"
          className="btn btn-secondary btn-sm conflict-statement__compare"
          onClick={() => {
            window.location.assign(compareHref)
          }}
        >
          Compare Sources
        </button>
      )}
    </div>
  )
}

/** Build the "Compare Sources" target for a statement pair (plan §22). */
export function compareSourcesHref(
  a: ConflictStatement,
  b: ConflictStatement,
  section?: string | null,
): string {
  const params = new URLSearchParams({
    documentA: a.document_id,
    documentB: b.document_id,
    versionA: a.document_version_id,
    versionB: b.document_version_id,
  })
  if (section) params.set('section', section)
  return `/app/compare?${params.toString()}`
}

export function ConflictCard({
  conflict,
  statements,
  canResolve,
  onOpen,
}: {
  conflict: ConflictSummary
  statements?: ConflictStatement[]
  canResolve: boolean
  onOpen?: () => void
}) {
  const navigate = useNavigate()
  const resolveMutation = useResolveConflict(conflict.id)

  const pairs: Array<[ConflictStatement, ConflictStatement] | null> = []
  if (statements && statements.length >= 2) {
    for (let i = 0; i + 1 < statements.length; i += 2) {
      pairs.push([statements[i], statements[i + 1]])
    }
  }
  const firstPair = pairs[0]
  const section = statements?.[0]?.section ?? null

  const likelyResolved = conflict.priority === 'LIKELY_RESOLVED'

  return (
    <article
      className={[
        'card conflict-card',
        likelyResolved ? 'conflict-card--likely-resolved' : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="conflict-card__header">
        <ConflictSeverityBadge severity={conflict.severity} />
        <button
          type="button"
          className="conflict-card__topic"
          onClick={onOpen ?? (() => navigate(`/app/conflicts/${conflict.id}`))}
        >
          {conflict.topic}
        </button>
        {likelyResolved && (
          <span className="conflict-card__hint" title="A newer version exists on at least one side">
            Likely resolved by version update
          </span>
        )}
        <span className="conflict-card__count">
          {conflict.statement_count} statement{conflict.statement_count === 1 ? '' : 's'}
        </span>
      </div>

      {statements && statements.length > 0 && (
        <div className="conflict-card__statements">
          {statements.map((statement) => (
            <ConflictStatementRow key={statement.id} statement={statement} />
          ))}
        </div>
      )}

      <div className="conflict-card__actions">
        {firstPair && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() =>
              navigate(compareSourcesHref(firstPair[0], firstPair[1], section))
            }
          >
            Compare Sources
          </button>
        )}
        {canResolve && conflict.status === 'OPEN' && (
          <ConflictResolutionMenu
            busy={resolveMutation.isPending}
            onResolve={(decision, note) =>
              resolveMutation.mutate({ decision, note })
            }
          />
        )}
        {conflict.status !== 'OPEN' && (
          <span className="conflict-card__resolved-tag">{conflict.status}</span>
        )}
      </div>
    </article>
  )
}
