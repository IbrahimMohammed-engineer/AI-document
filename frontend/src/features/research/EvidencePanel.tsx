/**
 * EvidencePanel — Research Workspace right panel (§6.5).
 *
 * Reads `panelStore.activeCitation`; fetches the cited page via
 * GET /documents/{id}/content and highlights the exact quoted span. The
 * snippet surface keeps the researcher in place — navigation to the full
 * Document Workspace is an explicit secondary action.
 */

import { useMemo } from 'react'

import { useDocumentContent } from '@/hooks/queries/useDocuments'
import { usePanelStore } from '@/store/panelStore'
import './research.css'

export function EvidencePanel() {
  const activeCitation = usePanelStore((s) => s.activeCitation)
  const contentQuery = useDocumentContent(
    activeCitation?.document_id ?? null,
    activeCitation?.page ?? null,
  )

  const highlighted = useMemo(() => {
    if (!contentQuery.data || !activeCitation) return null
    const text = contentQuery.data.text || ''
    const needle = activeCitation.text.trim()
    if (!needle) return { before: text, quote: '', after: '' }

    const at = text.indexOf(needle)
    if (at === -1) return null
    return {
      before: text.slice(Math.max(0, at - 200), at),
      quote: needle,
      after: text.slice(at + needle.length, at + needle.length + 400),
    }
  }, [contentQuery.data, activeCitation])

  if (!activeCitation) {
    return (
      <div className="research-evidence research-evidence--empty">
        <div className="empty-state">
          <div className="empty-state-title">No evidence selected</div>
          <p className="empty-state-description">
            Click a citation badge in the transcript to inspect its exact
            source here — without leaving your research.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="research-evidence" aria-live="polite" aria-label="Evidence panel">
      <div className="research-evidence-meta">
        <strong>{activeCitation.document_name}</strong>
        <span>
          Page {activeCitation.page}
          {activeCitation.section ? ` · ${activeCitation.section}` : ''}
          {activeCitation.version_number != null
            ? ` · v${activeCitation.version_number}`
            : ''}
        </span>
        <a
          href={`/app/documents/${activeCitation.document_id}?page=${activeCitation.page}&q=${encodeURIComponent(activeCitation.text)}`}
          onClick={(event) => {
            event.preventDefault()
            // SPA navigation without losing research state
            window.history.pushState({}, '', event.currentTarget.href)
            window.dispatchEvent(new PopStateEvent('popstate'))
          }}
        >
          Open in Document Workspace →
        </a>
      </div>

      {contentQuery.isLoading && (
        <div aria-busy="true">
          <span className="skeleton" style={{ height: '3rem', marginBottom: '0.5rem' }} />
          <span className="skeleton" style={{ height: '3rem' }} />
        </div>
      )}

      {contentQuery.isError && (
        <div className="text-sm text-muted">
          Could not load the source page — the quoted span below is from the
          answer's citation record.
          <p className="research-evidence-text" style={{ marginTop: '0.5rem' }}>
            <mark>“{activeCitation.text}”</mark>
          </p>
        </div>
      )}

      {contentQuery.data &&
        (highlighted ? (
          <p className="research-evidence-text">
            {highlighted.before && <span>…{highlighted.before}</span>}
            <mark>{highlighted.quote}</mark>
            {highlighted.after && <span>{highlighted.after}…</span>}
          </p>
        ) : (
          <p className="research-evidence-text">
            <mark>“{activeCitation.text}”</mark>
            <span className="text-xs text-muted">
              {' '}
              (exact span not found on the extracted page)
            </span>
          </p>
        ))}
    </div>
  )
}
