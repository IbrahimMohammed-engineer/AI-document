/**
 * Dashboard (Phase 15 §6.2) — all four KPI cards live, recent widgets, and
 * the new-organization empty state.
 *
 * KPI sources (live API, §6.2.1):
 *   Total Documents        → GET /documents?limit=1 → total
 *   Active Conversations   → GET /chat/conversations?limit=1 → total
 *   Documents Processing   → GET /documents/processing → total
 *   Open Conflicts         → GET /conflicts?status=OPEN → items.length
 *
 * Each KPI card handles its own loading (skeleton pulse) / error (— with
 * retry) state independently (state matrix FE §18). The new-org empty state
 * replaces the whole grid when the org has zero documents AND zero
 * conversations.
 */

import { Link } from 'react-router-dom'

import { UploadDialog } from '@/features/documents'
import {
  useProcessingJobs,
  useRetryProcessing,
} from '@/hooks/queries/useDocumentProcessing'
import { useDocumentList } from '@/hooks/queries/useDocuments'
import { useConflicts, useConflictScanStatus } from '@/hooks/queries/useConflicts'
import { useConversations } from '@/hooks/queries/useConversations'
import { JOB_TYPE_LABELS } from '@/features/documents/processingStatus'
import { useState } from 'react'

// ─── KPI card ─────────────────────────────────────────────────────────────────

function KpiCard({
  label,
  icon,
  to,
  value,
  hint,
  isLoading,
  isError,
  onRetry,
}: {
  label: string
  icon: string
  to?: string
  value: number | string | undefined
  hint?: string
  isLoading: boolean
  isError: boolean
  onRetry?: () => void
}) {
  const body = (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: '1.5rem' }} aria-hidden="true">
          {icon}
        </span>
      </div>
      <div
        style={{ fontSize: '1.75rem', fontWeight: 700, color: 'var(--color-neutral-800)' }}
        aria-live="polite"
      >
        {isLoading ? (
          <span className="skeleton" style={{ width: '3rem', height: '1.8rem' }} />
        ) : isError ? (
          <span title="Could not load" style={{ color: 'var(--color-neutral-400)' }}>
            —{' '}
            {onRetry && (
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={onRetry}
                aria-label={`Retry loading ${label}`}
              >
                ↻
              </button>
            )}
          </span>
        ) : (
          (value ?? '—')
        )}
      </div>
      <div className="text-sm text-muted">
        {label}
        {hint ? ` · ${hint}` : ''}
      </div>
    </>
  )

  const className = 'card kpi-card'
  if (to && !isLoading && !isError) {
    return (
      <Link to={to} className={className} style={{ textDecoration: 'none' }}>
        {body}
      </Link>
    )
  }
  return <div className={className}>{body}</div>
}

// ─── Widget shell ─────────────────────────────────────────────────────────────

function Widget({
  title,
  action,
  children,
}: {
  title: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="card dashboard-widget">
      <div className="dashboard-widget-head">
        <h2 className="section-title" style={{ margin: 0 }}>
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  )
}

function WidgetSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skeleton" style={{ height: '1.4rem', marginBottom: '0.5rem' }} />
      ))}
    </div>
  )
}

function WidgetError({ onRetry }: { onRetry: () => void }) {
  return (
    <div>
      <div className="text-sm text-muted" style={{ marginBottom: '0.5rem' }}>
        Could not load.
      </div>
      <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.round(diffMs / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const [uploadOpen, setUploadOpen] = useState(false)

  const documentsQuery = useDocumentList({ limit: 5 })
  const documentsTotalQuery = useDocumentList({ limit: 1 })
  const conversationsQuery = useConversations(5)
  const conversationsTotalQuery = useConversations(1)
  const processingQuery = useProcessingJobs()
  const openConflictsQuery = useConflicts('OPEN')
  const scanStatusQuery = useConflictScanStatus()
  const retryProcessing = useRetryProcessing()

  const documentsTotal = documentsTotalQuery.data?.total
  const questionsTotal = conversationsTotalQuery.data?.total
  const processingJobs = processingQuery.data?.items ?? []
  const inProgressJobs = processingJobs.filter(
    (job) => job.status === 'PENDING' || job.status === 'PROCESSING' || job.status === 'RETRYING',
  )
  const failedJobs = processingJobs.filter((job) => job.status === 'FAILED')

  // New-organization empty state (§6.2.5): zero docs AND zero questions
  const isNewOrganization =
    documentsTotalQuery.isSuccess &&
    conversationsTotalQuery.isSuccess &&
    documentsTotal === 0 &&
    questionsTotal === 0

  if (isNewOrganization) {
    return (
      <div>
        <div className="page-header">
          <div>
            <h1 className="page-title">Dashboard</h1>
            <p className="page-subtitle">
              Overview of your organization's document activity
            </p>
          </div>
        </div>
        <div className="card empty-state" style={{ padding: '3rem 1.5rem' }}>
          <div className="empty-state-icon" aria-hidden="true">
            ✦
          </div>
          <div className="empty-state-title">Welcome to your knowledge base</div>
          <p className="empty-state-description">
            Upload your first document to unlock grounded AI answers,
            summaries, comparisons and conflict detection.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setUploadOpen(true)}
          >
            Upload your first document
          </button>
        </div>
        <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} />
      </div>
    )
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-subtitle">
            Overview of your organization's document activity
          </p>
        </div>
      </div>

      {/* ── KPI cards ─────────────────────────────────────────────────────── */}
      <div className="dashboard-kpis">
        <KpiCard
          icon="📄"
          label="Total Documents"
          to="/app/documents"
          value={documentsTotal}
          isLoading={documentsTotalQuery.isLoading}
          isError={documentsTotalQuery.isError}
          onRetry={() => void documentsTotalQuery.refetch()}
        />
        <KpiCard
          icon="✦"
          label="Active Conversations"
          to="/app/ask"
          value={questionsTotal}
          isLoading={conversationsTotalQuery.isLoading}
          isError={conversationsTotalQuery.isError}
          onRetry={() => void conversationsTotalQuery.refetch()}
        />
        <KpiCard
          icon="⚙"
          label="Documents Processing"
          value={processingQuery.data?.total ?? 0}
          hint={
            inProgressJobs.length > 0
              ? `${inProgressJobs.length} active`
              : undefined
          }
          isLoading={processingQuery.isLoading}
          isError={processingQuery.isError}
          onRetry={() => void processingQuery.refetch()}
        />
        <KpiCard
          icon="⚠"
          label="Open Conflicts"
          to="/app/conflicts"
          value={
            openConflictsQuery.isSuccess ? openConflictsQuery.data?.items.length : undefined
          }
          hint={
            scanStatusQuery.data?.unscanned_document_count
              ? `${scanStatusQuery.data.unscanned_document_count} unscanned`
              : undefined
          }
          isLoading={openConflictsQuery.isLoading}
          isError={openConflictsQuery.isError}
          onRetry={() => void openConflictsQuery.refetch()}
        />
      </div>

      {/* ── Widgets ───────────────────────────────────────────────────────── */}
      <div className="dashboard-widgets">
        <Widget
          title="Recent Documents"
          action={
            <Link to="/app/documents" className="text-sm">
              View all
            </Link>
          }
        >
          {documentsQuery.isLoading ? (
            <WidgetSkeleton />
          ) : documentsQuery.isError ? (
            <WidgetError onRetry={() => void documentsQuery.refetch()} />
          ) : (documentsQuery.data?.items ?? []).length === 0 ? (
            <div className="text-sm text-muted">
              No documents yet —{' '}
              {documentsQuery.data?.total === 0 && (
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() => setUploadOpen(true)}
                >
                  upload your first
                </button>
              )}
            </div>
          ) : (
            <ul className="dashboard-list">
              {(documentsQuery.data?.items ?? []).map((doc) => (
                <li key={doc.id}>
                  <Link to={`/app/documents/${doc.id}`}>{doc.name}</Link>
                  <span className="dashboard-list-meta">
                    {doc.document_type} · {relativeTime(doc.updated_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Widget>

        <Widget
          title="Recent Questions"
          action={
            <Link to="/app/ask" className="text-sm">
              Ask AI
            </Link>
          }
        >
          {conversationsQuery.isLoading ? (
            <WidgetSkeleton />
          ) : conversationsQuery.isError ? (
            <WidgetError onRetry={() => void conversationsQuery.refetch()} />
          ) : (conversationsQuery.data?.items ?? []).length === 0 ? (
            <div className="text-sm text-muted">No questions asked yet.</div>
          ) : (
            <ul className="dashboard-list">
              {(conversationsQuery.data?.items ?? []).map((conversation) => (
                <li key={conversation.id}>
                  <Link to={`/app/ask?conversation=${conversation.id}`}>
                    {conversation.title ?? 'Untitled conversation'}
                  </Link>
                  <span className="dashboard-list-meta">
                    {relativeTime(conversation.updated_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Widget>

        <Widget title="Processing Activity">
          {processingQuery.isLoading ? (
            <WidgetSkeleton />
          ) : processingQuery.isError ? (
            <WidgetError onRetry={() => void processingQuery.refetch()} />
          ) : inProgressJobs.length === 0 && failedJobs.length === 0 ? (
            <div className="text-sm text-muted">
              No documents currently processing.
            </div>
          ) : (
            <ul className="dashboard-list">
              {[...inProgressJobs, ...failedJobs].slice(0, 5).map((job) => (
                <li key={job.job_id}>
                  <Link to={`/app/documents/${job.document_id}`}>{job.document_name}</Link>
                  <span className="dashboard-list-meta">
                    {JOB_TYPE_LABELS[job.job_type] ?? job.job_type} ·{' '}
                    {job.status === 'FAILED' ? (
                      <>
                        Failed{' '}
                        <button
                          type="button"
                          className="btn btn-ghost btn-xs"
                          onClick={() =>
                            retryProcessing.mutate(job.document_id, {
                              onSuccess: () => void processingQuery.refetch(),
                            })
                          }
                          disabled={retryProcessing.isPending}
                        >
                          Retry
                        </button>
                      </>
                    ) : (
                      (job.progress != null ? `${job.progress}%` : job.status.toLowerCase())
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Widget>
      </div>

      <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} />
    </div>
  )
}
