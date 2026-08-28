/**
 * ProcessingStatusTracker — the step-tracker for a document's pipeline.
 *
 * Driven by polling `GET /documents/{id}/status` via useDocumentStatus
 * (FE §6.4/§11). Renders every pipeline stage with the reached ones marked,
 * a progress bar with the worker's incremental progress, and the typed error
 * message plus the explicit Retry action when a stage has failed.
 */

import type { JobType } from '@/lib/api/documents'
import { useDocumentStatus, useRetryProcessing } from '@/hooks/queries/useDocumentProcessing'
import {
  JOB_STATUS_LABELS,
  JOB_TYPE_LABELS,
  PIPELINE_STEPS,
  VERSION_STATUS_LABELS,
  activeStepIndex,
  isInFlight,
} from './processingStatus'

interface ProcessingStatusTrackerProps {
  documentId: string
  /** Optionally render compact (no stage labels) inside tight layouts */
  compact?: boolean
  onRetryQueued?: () => void
}

export function ProcessingStatusTracker({
  documentId,
  compact = false,
  onRetryQueued,
}: ProcessingStatusTrackerProps) {
  const { data: status, isLoading, isError } = useDocumentStatus(documentId)
  const retry = useRetryProcessing()

  if (isLoading) {
    return <div className="processing-tracker processing-tracker--loading">Loading status…</div>
  }
  if (isError || !status) {
    return <div className="processing-tracker processing-tracker--error">Status unavailable.</div>
  }

  const inFlight = isInFlight(status.status)
  const reached = activeStepIndex(status.status, status.current_step)
  const progress =
    status.progress ?? (status.status === 'READY' ? 100 : inFlight ? null : 0)

  function handleRetry() {
    retry.mutate(documentId, {
      onSuccess: () => onRetryQueued?.(),
    })
  }

  return (
    <div className={`processing-tracker${compact ? ' processing-tracker--compact' : ''}`}>
      {/* ── Stage strip ─────────────────────────────────────────────────── */}
      <ol className="processing-steps" aria-label="Processing pipeline">
        {PIPELINE_STEPS.map((step, index) => {
          const state =
            step === 'READY'
              ? status.status === 'READY'
                ? 'done'
                : 'upcoming'
              : index < reached
                ? 'done'
                : index === reached
                  ? status.status === 'FAILED'
                    ? 'failed'
                    : 'current'
                  : 'upcoming'
          return (
            <li key={step} className={`processing-step is-${state}`}>
              <span className="processing-step-dot" aria-hidden="true" />
              {!compact && <span className="processing-step-label">{VERSION_STATUS_LABELS[step]}</span>}
            </li>
          )
        })}
      </ol>

      {/* ── Progress + live detail ──────────────────────────────────────── */}
      <div className="processing-detail">
        {inFlight && (
          <>
            <div className="processing-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress ?? undefined}>
              <div
                className="processing-progress-bar"
                style={{ width: `${progress ?? 8}%` }}
              />
            </div>
            <div className="processing-meta">
              {status.current_step && (
                <span className="processing-step-name">
                  {JOB_TYPE_LABELS[status.current_step as JobType]}
                  {status.job_status && (
                    <span className="processing-job-status">
                      {' '}
                      · {JOB_STATUS_LABELS[status.job_status]}
                    </span>
                  )}
                </span>
              )}
              {status.progress_message && (
                <span className="processing-progress-message">{status.progress_message}</span>
              )}
            </div>
          </>
        )}

        {status.status === 'READY' && (
          <div className="processing-meta processing-meta--ok">Processing complete.</div>
        )}

        {status.status === 'FAILED' && (
          <div className="processing-meta processing-meta--error">
            <span className="processing-error-message">
              {status.error_message ?? 'Processing failed.'}
            </span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={handleRetry}
              disabled={retry.isPending}
            >
              {retry.isPending ? 'Queueing…' : 'Retry processing'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
