/**
 * CitationList (FE §6.7/§12) — the "Sources" block under an answer when
 * citations exist.  One row per citation (multiple citations per claim stay
 * independent rows — never merged); hover previews the exact quoted span;
 * click navigates to the cited page with the highlight overlay.
 *
 * The Phase 9 plain source list remains as the fallback rendering when no
 * citation objects exist (e.g. a stopped/failed stream).
 */
import { useNavigate } from 'react-router-dom'

import type { AskCitation } from '@/lib/api/ask'
import { SourcePreview } from './SourcePreview'

export function CitationList({
  citations,
  onCitationClick,
}: {
  citations: AskCitation[]
  /** Phase 15 Research Workspace: intercept clicks (EvidencePanel sync). */
  onCitationClick?: (citation: AskCitation) => void
}) {
  const navigate = useNavigate()

  if (citations.length === 0) return null

  function openSource(citation: AskCitation) {
    if (onCitationClick) {
      onCitationClick(citation)
      return
    }
    const params = new URLSearchParams({
      page: String(citation.page),
      q: citation.text,
    })
    if (citation.version_number != null) {
      params.set('version', String(citation.version_number))
    }
    void navigate(`/app/documents/${citation.document_id}?${params.toString()}`)
  }

  return (
    <div className="ask-sources">
      <div className="ask-sources-title">Sources</div>
      {citations.map((citation) => (
        <button
          key={`${citation.index}-${citation.chunk_id}`}
          type="button"
          className="citation-row"
          aria-label={`Source ${citation.index}: ${citation.document_name}, page ${citation.page}`}
          onClick={() => openSource(citation)}
        >
          <span className="citation-row-index">[{citation.index}]</span>
          <span className="citation-row-body">
            <span className="citation-row-name">
              {citation.document_name}
              {citation.version_number != null ? ` · v${citation.version_number}` : ''}
            </span>
            <span className="citation-row-meta">
              Page {citation.page}
              {citation.section ? ` · ${citation.section}` : ''}
              {citation.effective_date ? ` · effective ${citation.effective_date}` : ''}
            </span>
            <span className="citation-row-quote">“{citation.text}”</span>
          </span>
          <SourcePreview citation={citation} />
        </button>
      ))}
    </div>
  )
}
