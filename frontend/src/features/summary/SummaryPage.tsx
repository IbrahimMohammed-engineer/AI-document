/**
 * SummaryPage (FE §6.12) — the cited, structured document summary screen.
 *
 * Layout: header (title + version + Regenerate, permission-gated), then
 * SummarySection blocks in the documented order (Executive Summary, Key
 * Points, two-column Dates/Roles, two-column Requirements/Risks, Topics).
 *
 * UX states (plan §6.8):
 *  - Loading: skeleton section blocks matching the layout (not one spinner).
 *  - Empty sections: explicit "No … identified in this document" per section.
 *  - Error: "Couldn't generate summary" + Retry; a prior summary payload
 *    stays visible with a warning banner instead of being replaced.
 *  - Partial (long-document sampling): inline disclosure banner reading the
 *    persisted sampling object.
 *  - Regenerating: existing content stays visible, dimmed, banner — never
 *    blank.
 */

import { Link, useParams } from 'react-router-dom'

import { useDocument } from '@/hooks/queries/useDocuments'
import { useSummary } from '@/hooks/queries/useSummary'
import type { SummaryPayload } from '@/lib/api/summary'

import { RegenerateButton } from './RegenerateButton'
import { SummarySection } from './SummarySection'
import { TopicTagList } from './TopicTagList'
import './summary.css'

function SummarySkeleton() {
  return (
    <div className="summary-body" aria-label="Loading summary">
      {[1, 2, 3, 4].map((block) => (
        <section key={block} className="summary-section summary-skeleton">
          <div className="summary-skeleton-line summary-skeleton-line--head" />
          <div className="summary-skeleton-line" />
          <div className="summary-skeleton-line summary-skeleton-line--short" />
        </section>
      ))}
    </div>
  )
}

export function SummaryPage() {
  const { id } = useParams<{ id: string }>()
  const documentId = id ?? null

  const { data: document } = useDocument(documentId)
  const { data: summary, isLoading, isError, refetch } = useSummary(documentId)

  const pending = summary?.status === 'PENDING' || summary?.status === 'PROCESSING'
  const failed = summary?.status === 'FAILED'
  const hasContent =
    summary?.summary != null && Object.keys(summary.summary).length > 0
  const sampling = summary?.sampling ?? null

  return (
    <div className="summary-page">
      <div className="page-header">
        <div>
          <div className="workspace-breadcrumb">
            <Link to={`/app/documents/${documentId ?? ''}`}>Document</Link>
            <span aria-hidden="true">/</span>
            <span>Summary</span>
          </div>
          <h1 className="page-title">
            Summary{document ? ` — ${document.name}` : ''}
            {document?.current_version
              ? ` (v${document.current_version.version_number})`
              : ''}
          </h1>
          {summary?.model && (
            <p className="page-subtitle">Generated with {summary.model}</p>
          )}
        </div>
        {documentId && (
          <RegenerateButton
            documentId={documentId}
            regenerating={pending}
          />
        )}
      </div>

      {sampling?.sampled && (
        <div className="summary-sampling-banner">
          Summary based on a representative sample across{' '}
          {sampling.included_section_ids.length} section
          {sampling.included_section_ids.length === 1 ? '' : 's'} (
          {sampling.strategy === 'section_diverse'
            ? 'section-diverse strategy'
            : sampling.strategy}
          ). Regenerate for a full pass once the full document fits the
          budget.
        </div>
      )}

      {summary?.stale && (
        <div className="summary-stale-banner" role="status">
          The document's content changed after this summary was generated —
          consider regenerating it.
        </div>
      )}

      {pending && hasContent && (
        <div className="summary-regen-banner" role="status">
          Regenerating… the previous summary stays visible below until the new
          one is ready.
        </div>
      )}

      {isLoading && <SummarySkeleton />}

      {isError && (
        <div className="card empty-state">
          <div className="empty-state-title">Couldn't generate summary</div>
          <p className="empty-state-description">
            Something went wrong while generating the summary. Your document
            is unaffected.
          </p>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      )}

      {failed && !hasContent && !isError && (
        <div className="card empty-state">
          <div className="empty-state-title">Couldn't generate summary</div>
          <p className="empty-state-description">
            {summary?.error_message ??
              'The generation job failed. You can retry.'}
          </p>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void refetch()}
          >
            Retry
          </button>
        </div>
      )}

      {pending && !hasContent && !isLoading && (
        <div className="summary-body">
          <SummarySkeleton />
        </div>
      )}

      {hasContent && (
        <div className={`summary-body${pending ? ' summary-body--dimmed' : ''}`}>
          {(() => {
            const payload: SummaryPayload = summary!.summary!
            return (
              <>
                <SummarySection
                  title="Executive Summary"
                  items={payload.executive_summary}
                  emptyMessage="No executive summary was produced for this document."
                />
                <SummarySection
                  title="Key Points"
                  items={payload.key_points}
                  emptyMessage="No key points identified in this document."
                />
                <div className="summary-grid">
                  <SummarySection
                    title="Dates"
                    items={payload.dates}
                    emptyMessage="No dates identified in this document."
                  />
                  <SummarySection
                    title="Roles"
                    items={payload.roles}
                    emptyMessage="No roles identified in this document."
                  />
                </div>
                <div className="summary-grid">
                  <SummarySection
                    title="Requirements"
                    items={payload.requirements}
                    emptyMessage="No requirements identified in this document."
                  />
                  <SummarySection
                    title="Risks"
                    items={payload.risks}
                    emptyMessage="No risks identified in this document."
                  />
                </div>
                <TopicTagList topics={payload.topics} />
              </>
            )
          })()}
        </div>
      )}
    </div>
  )
}
