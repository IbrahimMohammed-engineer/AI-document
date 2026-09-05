/**
 * SummarySection (FE §6.12) — one labeled summary block.
 *
 * Renders a list of {text, citation} items, each with a CitationBadge
 * REUSED from features/ask (never a second citation-badge component) —
 * the badge opens the same SourcePreview / document-navigation flow
 * already built for chat citations.
 *
 * Empty state (plan §6.8): a section with zero items renders its heading
 * plus the explicit "No {x} identified in this document" message — driven
 * directly by the persisted empty list, never an omitted key.
 */

import { CitationBadge } from '@/features/ask/CitationBadge'
import type { AskCitation } from '@/lib/api/ask'
import type { SummaryCitation, SummaryItem } from '@/lib/api/summary'

/** Adapt a persisted summary citation into the AskCitation shape the
 * shared CitationBadge/SourcePreview components consume (verbatim reuse —
 * no parallel component). */
function toAskCitation(
  citation: SummaryCitation,
  itemIndex: number,
): AskCitation {
  return {
    index: citation.index || itemIndex + 1,
    chunk_id: citation.chunk_id,
    document_id: citation.document_id,
    document_version_id: citation.document_version_id,
    document_name: citation.document_name,
    version_number: null,
    effective_date: null,
    page_id: citation.page_id,
    page: citation.page_number,
    section: citation.section,
    text: citation.quoted_text,
    char_start: citation.char_start,
    char_end: citation.char_end,
    context_before: '',
    context_after: '',
    relevance: citation.relevance,
  }
}

export function SummarySection({
  title,
  items,
  emptyMessage,
}: {
  title: string
  items: SummaryItem[]
  emptyMessage: string
}) {
  return (
    <section className="summary-section">
      <h2 className="summary-section-title">{title}</h2>
      {items.length === 0 ? (
        <p className="summary-empty">{emptyMessage}</p>
      ) : (
        <ul className="summary-list">
          {items.map((item, itemIndex) => (
            <li key={itemIndex} className="summary-item">
              <span className="summary-item-text">{item.text}</span>
              {item.citations.map((citation, citationIndex) => (
                <CitationBadge
                  key={`${citation.chunk_id}-${citationIndex}`}
                  citation={toAskCitation(citation, itemIndex)}
                />
              ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
