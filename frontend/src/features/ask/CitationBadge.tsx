/**
 * CitationBadge (FE §6.7/§12) — the numbered, focusable, aria-labeled inline
 * marker rendering a resolved citation.  Hover/focus shows the SourcePreview
 * popover (exact span in context); click navigates to the Document Workspace
 * at the cited page with the persistent source-highlight overlay.
 *
 * Muted "unresolved" state (FE §12): a marker the backend could not resolve
 * (citation data missing) renders dimmed and non-navigating — it can occur
 * only for sources deleted after an answer was persisted.
 */
import { useNavigate } from 'react-router-dom'

import type { AskCitation } from '@/lib/api/ask'
import { SourcePreview } from './SourcePreview'

export function CitationBadge({
  citation,
  onCitationClick,
}: {
  citation: AskCitation | null
  /** Phase 15 Research Workspace: intercept the click (EvidencePanel sync)
      instead of navigating away. */
  onCitationClick?: (citation: AskCitation) => void
}) {
  const navigate = useNavigate()

  if (!citation) {
    // Unresolved / source-deleted muted state — never fabricated navigation
    return (
      <span className="citation-badge citation-badge-unresolved" title="Source unavailable">
        ⌀
      </span>
    )
  }

  const label = `Citation ${citation.index}: ${citation.document_name}, page ${citation.page}${citation.section ? `, ${citation.section}` : ''}`

  function openSource() {
    if (onCitationClick) {
      onCitationClick(citation!)
      return
    }
    const params = new URLSearchParams({
      page: String(citation!.page),
      q: citation!.text,
    })
    if (citation!.version_number != null) {
      params.set('version', String(citation!.version_number))
    }
    void navigate(
      `/app/documents/${citation!.document_id}?${params.toString()}`,
    )
  }

  return (
    <button
      type="button"
      className="citation-badge"
      aria-label={label}
      title={label}
      onClick={openSource}
    >
      [{citation.index}]
      <SourcePreview citation={citation} />
    </button>
  )
}
