/**
 * ProcessingStatusBadge — compact status chip for Documents-table rows.
 *
 * Renders the version's pipeline status; "active" states pulse so users can
 * see work in progress at a glance (FE §6.4). Failed states offer the
 * explicit retry action (retry is never automatic — Backend §47).
 */

import type { VersionStatus } from '@/lib/api/documents'
import { useRetryProcessing } from '@/hooks/queries/useDocumentProcessing'
import { VERSION_STATUS_LABELS, versionStatusTone } from './processingStatus'

interface ProcessingStatusBadgeProps {
  documentId: string
  status: VersionStatus
  /** Show a retry button when failed (default true) */
  retryable?: boolean
  /** Called after a retry is accepted (202) */
  onRetryQueued?: () => void
}

export function ProcessingStatusBadge({
  documentId,
  status,
  retryable = true,
  onRetryQueued,
}: ProcessingStatusBadgeProps) {
  const retry = useRetryProcessing()
  const tone = versionStatusTone(status)

  return (
    <span className="processing-badge-group">
      <span className={`status-badge status-badge--${tone}${tone === 'active' ? ' pulse' : ''}`}>
        <span className="status-dot" aria-hidden="true" />
        {VERSION_STATUS_LABELS[status]}
      </span>
      {status === 'FAILED' && retryable && (
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={() => retry.mutate(documentId, { onSuccess: () => onRetryQueued?.() })}
          disabled={retry.isPending}
          title="Retry the failed processing stage"
        >
          {retry.isPending ? 'Retrying…' : 'Retry'}
        </button>
      )}
    </span>
  )
}
