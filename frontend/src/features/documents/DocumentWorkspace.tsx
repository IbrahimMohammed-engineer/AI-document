/**
 * DocumentWorkspace — the document detail screen (FE §6, Phases 5–6 slice).
 *
 * Phase 5 scope (roadmap frontend work):
 *  - Document header + pipeline step-tracker (real EXTRACTING/OCR steps)
 *  - Basic viewer rendering the original file via signed URL (PDF in an
 *    embedded frame; other types fall back to a download card — FE §13
 *    groundwork; the deepened viewer lands with the full workspace)
 *  - Extracted-pages panel with per-page OCR flags and the partial-OCR
 *    warning surface (page-level "OCR failed" markers from the backend)
 *
 * Phase 6 scope:
 *  - TOC panel rendering the real section tree, with the "No structure
 *    detected" empty state and page-navigation fallback (FE §6.5)
 *
 * Phase 10 scope (citation navigation, FE §6.7/§12):
 *  - `?page=N&q=<quoted span>` deep link from a CitationBadge/CitationList:
 *    the cited page auto-expands, scrolls into view, and the exact quoted
 *    span renders with a persistent source-highlight overlay (text-offset
 *    based — the same real source text the backend persisted).
 *
 * Search-inside deepens in later phases on this skeleton.
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import {
  ProcessingStatusBadge,
  ProcessingStatusTracker,
  TocPanel,
} from '@/features/documents'
import {
  useDocument,
  useDocumentPages,
  useDocumentToc,
  useSignedUrl,
} from '@/hooks/queries/useDocuments'
import type { DocumentPageItem } from '@/lib/api/documents'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function DocumentWorkspace() {
  const { id } = useParams<{ id: string }>()
  const documentId = id ?? null
  const [searchParams] = useSearchParams()

  // Citation deep-link (FE §12): ?page=N&q=<quoted span> — the cited page
  // expands with a persistent highlight overlay on the exact source span.
  const focusPage = (() => {
    const raw = searchParams.get('page')
    const parsed = raw != null ? Number.parseInt(raw, 10) : Number.NaN
    return Number.isFinite(parsed) && parsed >= 1 ? parsed : null
  })()
  const highlightText = searchParams.get('q')

  const { data: document, isLoading, isError } = useDocument(documentId)
  const { data: pagesData } = useDocumentPages(documentId)
  const versionStatus = document?.current_version?.status
  const pipelineActive =
    versionStatus != null &&
    versionStatus !== 'READY' &&
    versionStatus !== 'FAILED' &&
    versionStatus !== 'UPLOADED'
  const { data: tocData, isLoading: tocLoading } = useDocumentToc(documentId, {
    pipelineActive,
  })

  if (isLoading) {
    return <div className="page-loading">Loading document…</div>
  }
  if (isError || !document) {
    return (
      <div className="card empty-state">
        <div className="empty-state-title">Document not found</div>
        <Link to="/app/documents" className="btn btn-secondary btn-sm">
          Back to documents
        </Link>
      </div>
    )
  }

  const failedPages = (pagesData?.items ?? []).filter((p) => p.ocr_failed)
  const partialOcr = failedPages.length > 0

  return (
    <div className="document-workspace">
      <div className="page-header">
        <div>
          <div className="workspace-breadcrumb">
            <Link to="/app/documents">Documents</Link>
            <span aria-hidden="true">/</span>
            <span>{document.name}</span>
          </div>
          <h1 className="page-title">{document.name}</h1>
          <p className="page-subtitle">
            {document.document_type}
            {document.department ? ` · ${document.department}` : ''}
            {document.current_version &&
              ` · v${document.current_version.version_number} · ${formatBytes(
                document.current_version.file_size_bytes,
              )}`}
          </p>
        </div>
        {document.current_version && (
          <ProcessingStatusBadge
            status={document.current_version.status}
            documentId={document.id}
          />
        )}
      </div>

      <section className="card workspace-processing">
        <ProcessingStatusTracker documentId={document.id} />
      </section>

      {partialOcr && (
        <div className="partial-ocr-warning" role="alert">
          <strong>Partial OCR:</strong>{' '}
          {failedPages.length === 1 ? '1 page' : `${failedPages.length} pages`}{' '}
          could not be read (
          {failedPages.map((p) => `p.${p.page_number}`).join(', ')}). The
          document was processed without{' '}
          {failedPages.length === 1 ? 'it' : 'them'} — those pages are absent
          from search and AI answers.
        </div>
      )}

      <div className="workspace-grid">
        <DocumentViewer
          documentId={document.id}
          mimeType={document.current_version?.mime_type}
        />
        <div className="workspace-side">
          <TocPanel
            items={tocData?.items ?? []}
            hasStructure={tocData?.has_structure ?? false}
            sectionCount={tocData?.section_count ?? 0}
            isLoading={tocLoading}
          />
          <PagesPanel
            pages={pagesData?.items ?? []}
            total={pagesData?.total ?? 0}
            pageCount={pagesData?.page_count ?? null}
            focusPage={focusPage}
            highlightText={highlightText}
          />
        </div>
      </div>
    </div>
  )
}

// ─── Viewer (signed-URL groundwork, FE §13) ───────────────────────────────────

function DocumentViewer({
  documentId,
  mimeType,
}: {
  documentId: string
  mimeType?: string
}) {
  const [frameKey, setFrameKey] = useState(0)
  const { data: signed, isLoading, isError, refetch } = useSignedUrl(documentId)
  const isPdf = mimeType === 'application/pdf'

  return (
    <section className="card workspace-viewer" aria-label="Document viewer">
      <div className="workspace-viewer-head">
        <h2 className="section-title">Original file</h2>
        {isPdf && (
          <div className="workspace-viewer-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void refetch()
                setFrameKey((k) => k + 1)
              }}
            >
              Refresh link
            </button>
            {signed && (
              <a
                className="btn btn-secondary btn-sm"
                href={signed.url}
                target="_blank"
                rel="noreferrer"
              >
                Open in new tab
              </a>
            )}
          </div>
        )}
      </div>

      {isPdf ? (
        isLoading ? (
          <div className="viewer-placeholder">Signing download link…</div>
        ) : isError || !signed ? (
          <div className="viewer-placeholder viewer-placeholder--error">
            Could not sign the file URL.{' '}
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => void refetch()}
            >
              Try again
            </button>
          </div>
        ) : (
          <iframe
            key={frameKey}
            src={signed.url}
            title="Document viewer"
            className="viewer-frame"
          />
        )
      ) : (
        <div className="viewer-placeholder">
          Inline preview for this file type arrives with the full workspace.
          Download the original to view it.
        </div>
      )}
    </section>
  )
}

// ─── Extracted pages panel (Phase 5 tooling + Phase 10 citation highlight) ───

/**
 * Render page text with the cited span wrapped in a persistent highlight.
 * The quoted text is the REAL source text persisted by the backend, so a
 * plain substring search locates it (first occurrence wins — chunks are
 * single-topic, collisions are rare and benign).
 */
function HighlightedPageText({
  text,
  highlight,
}: {
  text: string
  highlight: string
}) {
  const needle = highlight.trim()
  if (!needle) return <>{text}</>

  const at = text.indexOf(needle)
  if (at === -1) return <>{text}</>

  return (
    <>
      {text.slice(0, at)}
      <mark className="source-highlight">{needle}</mark>
      {text.slice(at + needle.length)}
    </>
  )
}

function PagesPanel({
  pages,
  total,
  pageCount,
  focusPage = null,
  highlightText = null,
}: {
  pages: DocumentPageItem[]
  total: number
  pageCount: number | null
  /** Citation deep-link: page to auto-expand + scroll to (FE §12). */
  focusPage?: number | null
  /** Citation deep-link: exact quoted span to highlight on that page. */
  highlightText?: string | null
}) {
  const [expandedPage, setExpandedPage] = useState<number | null>(focusPage)
  const focusRef = useRef<HTMLLIElement | null>(null)
  const scrolledRef = useRef(false)

  useEffect(() => {
    if (focusPage == null || scrolledRef.current) return
    // Wait until the pages data has actually arrived before scrolling.
    if (pages.length === 0) return
    scrolledRef.current = true
    requestAnimationFrame(() => {
      focusRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [focusPage, pages.length])

  return (
    <section className="card workspace-pages" aria-label="Extracted pages">
      <div className="workspace-pages-head">
        <h2 className="section-title">Extracted pages</h2>
        <span className="workspace-pages-count">
          {pageCount != null && total < pageCount
            ? `${total} / ${pageCount} extracted…`
            : `${total} page${total === 1 ? '' : 's'}`}
        </span>
      </div>

      {highlightText && focusPage != null && (
        <div className="citation-focus-banner">
          Showing the passage cited in your answer (page {focusPage}).
        </div>
      )}

      {total === 0 ? (
        <div className="empty-state">
          <div className="empty-state-title">No pages yet</div>
          <p className="empty-state-description">
            Extracted text appears here as the processing pipeline runs.
          </p>
        </div>
      ) : (
        <ol className="pages-list">
          {pages.map((page) => {
            const expanded = expandedPage === page.page_number
            const snippet = page.text.trim().slice(0, 160)
            const isFocus = focusPage === page.page_number
            return (
              <li
                key={page.page_number}
                className={`pages-item${isFocus ? ' pages-item-focus' : ''}`}
                ref={isFocus ? focusRef : undefined}
              >
                <button
                  type="button"
                  className="pages-item-head"
                  onClick={() =>
                    setExpandedPage(expanded ? null : page.page_number)
                  }
                  aria-expanded={expanded}
                >
                  <span className="pages-item-number">p. {page.page_number}</span>
                  {page.ocr_failed ? (
                    <span className="status-badge status-badge--error">
                      OCR failed
                    </span>
                  ) : page.ocr_used ? (
                    <span className="status-badge status-badge--active">OCR</span>
                  ) : (
                    <span className="status-badge status-badge--neutral">Text</span>
                  )}
                  <span className="pages-item-snippet">
                    {snippet || (page.ocr_failed ? 'No text — OCR failed' : '(empty page)')}
                  </span>
                </button>
                {expanded && (
                  <pre className="pages-item-text">
                    {isFocus && highlightText ? (
                      <HighlightedPageText text={page.text || ''} highlight={highlightText} />
                    ) : (
                      page.text || '(no text)'
                    )}
                  </pre>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
