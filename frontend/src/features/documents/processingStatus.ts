/**
 * Processing-status presentation helpers (FE §6.4 / §11).
 *
 * Single source of truth for how pipeline states render across the badge,
 * the step tracker, and the header indicator.
 */

import type { JobStatus, JobType, VersionStatus } from '@/lib/api/documents'

/** Ordered pipeline stages for the step tracker (Backend §47). */
export const PIPELINE_STEPS: VersionStatus[] = [
  'UPLOADED',
  'PROCESSING',
  'EXTRACTING',
  'OCR',
  'CHUNKING',
  'EMBEDDING',
  'INDEXING',
  'READY',
]

/** Human labels for version pipeline states. */
export const VERSION_STATUS_LABELS: Record<VersionStatus, string> = {
  UPLOADED: 'Uploaded',
  PROCESSING: 'Processing',
  EXTRACTING: 'Extracting',
  OCR: 'OCR',
  CHUNKING: 'Chunking',
  EMBEDDING: 'Embedding',
  INDEXING: 'Indexing',
  READY: 'Ready',
  FAILED: 'Failed',
}

/** Human labels for job types (the "current step" line). */
export const JOB_TYPE_LABELS: Record<JobType, string> = {
  EXTRACTION: 'Text extraction',
  OCR: 'OCR scan',
  CHUNKING: 'Structure & chunking',
  EMBEDDING: 'Embeddings',
  INDEXING: 'Indexing',
  COMPARISON: 'Version comparison',
  SUMMARY: 'Summary generation',
  CONFLICT_SCAN: 'Conflict scan',
  PURGE: 'Cleanup',
}

/** Human labels for job states. */
export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  PENDING: 'Queued',
  PROCESSING: 'Running',
  RETRYING: 'Retrying',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
}

export type StatusTone = 'neutral' | 'active' | 'success' | 'error'

/** Visual tone for a version status (drives badge CSS classes). */
export function versionStatusTone(status: VersionStatus): StatusTone {
  switch (status) {
    case 'READY':
      return 'success'
    case 'FAILED':
      return 'error'
    case 'UPLOADED':
      return 'neutral'
    default:
      return 'active' // PROCESSING / EXTRACTING / OCR / CHUNKING / EMBEDDING / INDEXING
  }
}

/** Visual tone for a job status. */
export function jobStatusTone(status: JobStatus): StatusTone {
  switch (status) {
    case 'COMPLETED':
      return 'success'
    case 'FAILED':
      return 'error'
    case 'PENDING':
      return 'neutral'
    default:
      return 'active' // PROCESSING / RETRYING
  }
}

/** True while the version is still moving through the pipeline. */
export function isInFlight(status: VersionStatus): boolean {
  return status !== 'READY' && status !== 'FAILED'
}

/** Map a live job type onto its pipeline stage (for tracker highlighting). */
const JOB_TYPE_TO_STAGE: Partial<Record<JobType, VersionStatus>> = {
  EXTRACTION: 'EXTRACTING',
  OCR: 'OCR',
  CHUNKING: 'CHUNKING',
  EMBEDDING: 'EMBEDDING',
  INDEXING: 'INDEXING',
}

/** Which pipeline step the version has reached (tracker highlight index). */
export function activeStepIndex(
  status: VersionStatus,
  currentStep: JobType | null,
): number {
  if (status === 'READY') return PIPELINE_STEPS.length - 1
  const stage = (currentStep && JOB_TYPE_TO_STAGE[currentStep]) || status
  const index = PIPELINE_STEPS.indexOf(stage)
  return index >= 0 ? index : 0
}
