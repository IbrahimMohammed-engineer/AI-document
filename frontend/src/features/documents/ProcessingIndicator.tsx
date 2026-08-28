/**
 * ProcessingIndicator — global header widget (FE §5.2).
 *
 * Polls the org-wide active jobs (GET /documents/processing) and shows a live
 * count + per-item detail while anything is running. Hidden when idle.
 * Phase 11 upgrades this to the SSE stream; the presentation stays the same.
 */

import { useProcessingJobs } from '@/hooks/queries/useDocumentProcessing'
import { useAuthStore } from '@/store/authStore'
import { JOB_STATUS_LABELS, JOB_TYPE_LABELS } from './processingStatus'

export function ProcessingIndicator() {
  const accessToken = useAuthStore((s) => s.accessToken)
  const { data } = useProcessingJobs()

  if (!accessToken || !data || data.total === 0) return null

  return (
    <div className="processing-indicator" aria-live="polite">
      <span className="processing-indicator-count" title="Documents currently processing">
        <span className="status-dot pulse" aria-hidden="true" />
        {data.total} processing
      </span>
      <ul className="processing-indicator-list">
        {data.items.slice(0, 3).map((job) => (
          <li key={job.job_id} className="processing-indicator-item">
            <span className="processing-indicator-name" title={job.document_name}>
              {job.document_name}
            </span>
            <span className="processing-indicator-meta">
              {JOB_TYPE_LABELS[job.job_type]} · {JOB_STATUS_LABELS[job.status]}
              {job.progress != null ? ` · ${job.progress}%` : ''}
            </span>
          </li>
        ))}
        {data.items.length > 3 && (
          <li className="processing-indicator-more">+{data.items.length - 3} more</li>
        )}
      </ul>
    </div>
  )
}
