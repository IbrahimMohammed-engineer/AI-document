/**
 * ExtractionDetailPage (Phase 14) — one run's items grouped into four
 * labeled sections (Requirements, Risks, Dates, Parties).
 *
 * Each item carries a CitationBadge REUSED from features/ask (the same
 * SummarySection-style rendering pattern — not a parallel component).
 * Categories with zero items render the explicit empty state (plan §6.8).
 */

import { Link, useParams } from 'react-router-dom'

import { CitationBadge } from '@/features/ask/CitationBadge'
import { useExtractionRun } from '@/hooks/queries/useExtractions'
import { useDocument } from '@/hooks/queries/useDocuments'
import type { AskCitation } from '@/lib/api/ask'
import type { ExtractionItem } from '@/lib/api/extraction'

import './extraction.css'

function itemToAskCitation(item: ExtractionItem): AskCitation {
  return {
    index: item.item_index + 1,
    chunk_id: item.chunk_id,
    document_id: '',
    document_version_id: '',
    document_name: '',
    version_number: null,
    effective_date: null,
    page_id: item.page_id,
    page: item.page_number,
    section: item.section,
    text: item.quoted_text,
    char_start: item.char_start,
    char_end: item.char_end,
    context_before: '',
    context_after: '',
    relevance: item.relevance_score ?? 0,
  }
}

const CATEGORY_SECTIONS: Array<{
  key: 'requirement' | 'risk' | 'date' | 'party'
  title: string
  empty: string
}> = [
  {
    key: 'requirement',
    title: 'Requirements',
    empty: 'No requirements identified in this document.',
  },
  { key: 'risk', title: 'Risks', empty: 'No risks identified in this document.' },
  { key: 'date', title: 'Dates', empty: 'No dates identified in this document.' },
  {
    key: 'party',
    title: 'Parties',
    empty: 'No parties identified in this document.',
  },
]

export function ExtractionDetailPage() {
  const { extractionId } = useParams<{ extractionId: string }>()
  const { data: run, isLoading, isError } = useExtractionRun(extractionId)
  const { data: document } = useDocument(run?.document_id)

  if (isLoading) {
    return <div className="page-loading">Loading extraction run…</div>
  }
  if (isError || !run) {
    return (
      <div className="card empty-state">
        <div className="empty-state-title">Extraction run not found</div>
      </div>
    )
  }

  const pending = run.status === 'PENDING' || run.status === 'PROCESSING'
  const failed = run.status === 'FAILED'

  return (
    <div className="extraction-detail">
      <div className="page-header">
        <div>
          <div className="workspace-breadcrumb">
            <Link to={`/app/documents/${run.document_id}/extractions`}>
              Extractions
            </Link>
            <span aria-hidden="true">/</span>
            <span>Run</span>
          </div>
          <h1 className="page-title">
            Extraction run{document ? ` — ${document.name}` : ''}
          </h1>
          <p className="page-subtitle">
            Status: {run.status}
            {run.completed_at ? ` · completed ${new Date(run.completed_at).toLocaleString()}` : ''}
          </p>
        </div>
      </div>

      {pending && (
        <div className="extraction-banner" role="status">
          Extraction is running — this page updates automatically.
        </div>
      )}
      {failed && (
        <div className="card empty-state">
          <div className="empty-state-title">Extraction run failed</div>
          <p className="empty-state-description">
            {run.error_message ?? 'The extraction job failed. You can start a new run.'}
          </p>
        </div>
      )}

      {run.status === 'COMPLETED' && run.items && (
        <div className="extraction-body">
          {CATEGORY_SECTIONS.map(({ key, title, empty }) => {
            const items = run.items?.[key] ?? []
            return (
              <section key={key} className="summary-section">
                <h2 className="summary-section-title">
                  {title}
                  <span className="extraction-count">
                    {items.length} item{items.length === 1 ? '' : 's'}
                  </span>
                </h2>
                {items.length === 0 ? (
                  <p className="summary-empty">{empty}</p>
                ) : (
                  <ul className="summary-list">
                    {items.map((item) => (
                      <li key={item.id} className="summary-item">
                        <span className="summary-item-text">{item.label}</span>
                        <CitationBadge
                          citation={{
                            ...itemToAskCitation(item),
                            document_id: run.document_id,
                            document_version_id: run.document_version_id,
                            document_name: document?.name ?? '',
                          }}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}
