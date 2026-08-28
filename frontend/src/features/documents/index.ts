/**
 * Document processing UI (Phases 4–6).
 *
 * Building blocks consumed by the Documents screens:
 *  - ProcessingStatusBadge    — table-row status chip (+ retry on FAILED)
 *  - ProcessingStatusTracker  — pipeline step-tracker driven by polling
 *  - ProcessingIndicator      — global header widget (org-wide active jobs)
 *  - DocumentWorkspace        — document detail screen (Phase 5: viewer via
 *                               signed URL + extracted-pages panel + partial-
 *                               OCR warning; Phase 6: real TOC panel)
 *  - TocPanel                 — section tree (Phase 6) with the "No structure
 *                               detected" fallback state (FE §6.5)
 */
export { ProcessingStatusBadge } from './ProcessingStatusBadge'
export { ProcessingStatusTracker } from './ProcessingStatusTracker'
export { ProcessingIndicator } from './ProcessingIndicator'
export { DocumentWorkspace } from './DocumentWorkspace'
export { TocPanel } from './TocPanel'
export {
  JOB_STATUS_LABELS,
  JOB_TYPE_LABELS,
  PIPELINE_STEPS,
  VERSION_STATUS_LABELS,
  isInFlight,
  jobStatusTone,
  versionStatusTone,
} from './processingStatus'
export type { StatusTone } from './processingStatus'
import './processing.css'
