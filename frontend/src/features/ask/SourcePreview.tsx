/**
 * SourcePreview popover (FE §6.7/§12) — the cited source span shown in its
 * immediate context.  Rendered inside a CitationBadge on hover/focus; pure
 * presentation, ≤150 ms target (no network — everything ships in the done
 * payload from the backend's own citation record).
 */
import type { AskCitation } from '@/lib/api/ask'

export function SourcePreview({ citation }: { citation: AskCitation }) {
  const {
    context_before: before,
    text,
    context_after: after,
    document_name: documentName,
    version_number: version,
    page,
    section,
  } = citation

  return (
    <span
      className="source-preview"
      role="tooltip"
      onClick={(e) => e.stopPropagation()}
    >
      <span className="source-preview-body">
        {before && <span className="source-preview-context">{before}</span>}
        <mark className="source-preview-quote">{text}</mark>
        {after && <span className="source-preview-context">{after}</span>}
      </span>
      <span className="source-preview-meta">
        {documentName}
        {version != null ? ` · v${version}` : ''} · Page {page}
        {section ? ` · ${section}` : ''}
      </span>
    </span>
  )
}
