/**
 * SearchResultCard (Phase 15 §6.6) — one ranked result: section title,
 * snippet with the query terms highlighted, document name → deep link to
 * the page with highlight.
 */

import { Link } from 'react-router-dom'

import type { SearchResultItem } from '@/lib/api/search'

/** Split the snippet into plain/highlighted segments (case-insensitive). */
function highlightSnippet(snippet: string, query: string) {
  const terms = query
    .trim()
    .split(/\s+/)
    .filter((term) => term.length >= 2)
    .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  if (terms.length === 0) return [{ text: snippet, hit: false }]

  const pattern = new RegExp(`(${terms.join('|')})`, 'gi')
  return snippet
    .split(pattern)
    .filter((part) => part !== '')
    .map((part) => ({ text: part, hit: pattern.test(part) && part.length >= 2 }))
}

export function SearchResultCard({
  result,
  query,
}: {
  result: SearchResultItem
  query: string
}) {
  const segments = highlightSnippet(result.snippet, query)

  return (
    <article className="card search-result-card">
      {result.section_title && (
        <h3 className="search-result-section">{result.section_title}</h3>
      )}
      <p className="search-result-snippet">
        {segments.map((segment, i) =>
          segment.hit ? (
            <mark key={i}>{segment.text}</mark>
          ) : (
            <span key={i}>{segment.text}</span>
          ),
        )}
      </p>
      <div className="search-result-meta">
        <Link
          to={`/app/documents/${result.document_id}?page=${result.page_number}&q=${encodeURIComponent(query)}`}
          className="search-result-doc"
        >
          {result.document_name}
        </Link>
        <span className="text-xs text-muted">
          Page {result.page_number} · {(result.relevance * 100).toFixed(0)}% match
        </span>
      </div>
    </article>
  )
}
