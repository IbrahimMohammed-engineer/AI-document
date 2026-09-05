/**
 * DocumentWorkspace — the document detail screen (Phases 5–6 + Phase 15).
 *
 * Phase 15 (§6.4) upgrades over the Phase 5/6 slice:
 *  - PDF.js viewer (canvas + text layer) replaces the signed-URL <iframe>
 *  - Version selector (`?version=N`) — switching reloads viewer, pages, TOC
 *  - In-document search (Ctrl+F/⌘F) with F3/Shift+F3 match stepping
 *  - TOC scrollspy driven by the viewer's current page
 *  - Zoom controls (fit-width / fit-page / 50–150%)
 *  - 403 renders <PermissionDenied> (state matrix FE §18)
 *
 * Retained from prior phases: pipeline step-tracker, extracted-pages panel
 * with partial-OCR warning, `?page=N&q=` citation deep links, Summary and
 * Extractions entry points.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { PermissionDenied } from '@/components/PermissionDenied'
import {
  DocumentSearchBar,
  ProcessingStatusBadge,
  ProcessingStatusTracker,
  PdfViewer,
  TocPanel,
} from '@/features/documents'
import {
  useDocument,
  useDocumentPages,
  useDocumentToc,
  useDocumentVersions,
  useSignedUrl,
} from '@/hooks/queries/useDocuments'
import { useDocumentConflictSections } from '@/hooks/queries/useConflicts'
import { ApiError } from '@/lib/api/client'
import type { SearchMatch, ZoomMode } from '@/features/documents/PdfViewer'
import type { DocumentDetail, DocumentPageItem } from '@/lib/api/documents'
import './pdfViewer.css'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Parse `?page=N` defensively — deep links are untrusted input (§10). */
function parsePageParam(raw: string | null): number | null {
  if (raw == null) return null
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed >= 1 && parsed <= 100_000 ? parsed : null
}

export function DocumentWorkspace() {
  const { id } = useParams<{ id: string }>()
  const documentId = id ?? null
  const [searchParams, setSearchParams] = useSearchParams()

  // Citation deep-link (FE §12): ?page=N&q=<quoted span>
  const focusPage = parsePageParam(searchParams.get('page'))
  const highlightText = searchParams.get('q')
  // Version switching (Phase 15 §6.4.2): ?version=N (null = current)
  const versionParam = (() => {
    const raw = searchParams.get('version')
    if (raw == null) return null
    const parsed = Number.parseInt(raw, 10)
    return Number.isFinite(parsed) && parsed >= 1 ? parsed : null
  })()

  const { data: document, isLoading, isError, error } = useDocument(documentId)

  // 403 → explicit permission-denied state (never a generic error card)
  if (isError && error instanceof ApiError && error.isForbidden) {
    return <PermissionDenied message="You don't have access to this document." />
  }

  return (
    <DocumentWorkspaceBody
      documentId={documentId}
      doc={document ?? null}
      isLoading={isLoading}
      isError={isError}
      focusPage={focusPage}
      highlightText={highlightText}
      versionParam={versionParam}
      onVersionChange={(version) => {
        const next = new URLSearchParams(searchParams)
        next.set('version', String(version))
        next.delete('page')
        next.delete('q')
        setSearchParams(next)
      }}
      onJumpToPage={(page, q) => {
        const next = new URLSearchParams(searchParams)
        next.set('page', String(page))
        if (q) next.set('q', q)
        else next.delete('q')
        setSearchParams(next)
      }}
    />
  )
}

function DocumentWorkspaceBody({
  documentId,
  doc,
  isLoading,
  isError,
  focusPage,
  highlightText,
  versionParam,
  onVersionChange,
  onJumpToPage,
}: {
  documentId: string | null
  doc: DocumentDetail | null
  isLoading: boolean
  isError: boolean
  focusPage: number | null
  highlightText: string | null
  versionParam: number | null
  onVersionChange: (version: number) => void
  onJumpToPage: (page: number, q?: string) => void
}) {
  const navigate = useNavigate()
  const versionsQuery = useDocumentVersions(documentId)

  const { data: pagesData } = useDocumentPages(documentId, {
    version: versionParam ?? undefined,
    refetchWhileProcessing: versionParam == null,
  })
  const versionStatus = doc?.current_version?.status
  const pipelineActive =
    versionStatus != null &&
    versionStatus !== 'READY' &&
    versionStatus !== 'FAILED' &&
    versionStatus !== 'UPLOADED'
  const { data: tocData, isLoading: tocLoading } = useDocumentToc(documentId, {
    pipelineActive,
    version: versionParam ?? undefined,
  })
  // Phase 13 — unresolved-conflict markers in the TOC (FE §6.5 `[•]`)
  const { conflictSectionIds } = useDocumentConflictSections(documentId)

  // ── Viewer state ──────────────────────────────────────────────────────────
  const [currentPage, setCurrentPage] = useState(focusPage ?? 1)
  const [totalPages, setTotalPages] = useState<number | null>(null)
  const [zoom, setZoom] = useState<ZoomMode>('fit-width')

  // In-document search state
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState('')
  const [matches, setMatches] = useState<SearchMatch[]>([])
  const [matchIndex, setMatchIndex] = useState(-1)

  function stepMatch(delta: 1 | -1) {
    if (matches.length === 0) return
    setMatchIndex((index) => {
      const next = index + delta
      if (next < 0) return matches.length - 1
      if (next >= matches.length) return 0
      return next
    })
  }

  // Ctrl+F / ⌘F opens the in-document search; F3/Shift+F3 step matches
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        setSearchOpen(true)
      } else if (event.key === 'F3' && searchOpen) {
        event.preventDefault()
        if (event.shiftKey) stepMatch(-1)
        else stepMatch(1)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchOpen, matches, matchIndex])

  // Follow the active match across pages
  useEffect(() => {
    const match = matches[matchIndex]
    if (match && match.page !== currentPage) {
      setCurrentPage(match.page)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchIndex, matches])

  const handleMatchesFound = useCallback((found: SearchMatch[]) => {
    setMatches(found)
    setMatchIndex(found.length > 0 ? 0 : -1)
  }, [])

  // After the initial deep-link page renders, run the ?q= highlight
  const handleTextLayerReady = useCallback(() => {
    if (highlightText && !searchTerm) {
      setSearchTerm(highlightText)
      setSearchOpen(true)
    }
  }, [highlightText, searchTerm])

  const failedPages = useMemo(
    () => (pagesData?.items ?? []).filter((p: DocumentPageItem) => p.ocr_failed),
    [pagesData],
  )
  const partialOcr = failedPages.length > 0

  if (isLoading) {
    return (
      <div aria-busy="true">
        <div className="skeleton" style={{ height: '2rem', width: '40%', marginBottom: '1rem' }} />
        <div className="skeleton" style={{ height: '24rem' }} />
      </div>
    )
  }
  if (isError || !doc) {
    return (
      <div className="card empty-state">
        <div className="empty-state-title">Document not found</div>
        <Link to="/app/documents" className="btn btn-secondary btn-sm">
          Back to documents
        </Link>
      </div>
    )
  }

  const versions = versionsQuery.data?.items ?? []
  const isPdf = doc.current_version?.mime_type === 'application/pdf'

  return (
    <div className="document-workspace">
      <div className="page-header">
        <div>
          <div className="workspace-breadcrumb">
            <Link to="/app/documents">Documents</Link>
            <span aria-hidden="true">/</span>
            <span>{doc.name}</span>
          </div>
          <h1 className="page-title" tabIndex={-1}>
            {doc.name}
          </h1>
          <p className="page-subtitle">
            {doc.document_type}
            {doc.department ? ` · ${doc.department}` : ''}
            {doc.current_version &&
              ` · v${doc.current_version.version_number} · ${formatBytes(
                doc.current_version.file_size_bytes,
              )}`}
          </p>
        </div>
        {doc.current_version && (
          <ProcessingStatusBadge
            status={doc.current_version.status}
            documentId={doc.id}
          />
        )}
      </div>

      {/* Phase 14 — action bar (FE §6.5) */}
      <div className="workspace-actions">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => navigate(`/app/documents/${doc.id}/summary`)}
        >
          Summary
        </button>
        <Link
          to={`/app/documents/${doc.id}/extractions`}
          className="btn btn-secondary btn-sm"
        >
          Extractions
        </Link>

        {/* Phase 15 §6.4.2 — version selector */}
        {versions.length > 1 && (
          <div className="version-selector" style={{ marginLeft: 'auto' }}>
            <label htmlFor="version-select">Version</label>
            <select
              id="version-select"
              value={versionParam ?? doc.current_version?.version_number ?? ''}
              onChange={(event) => onVersionChange(Number(event.target.value))}
            >
              {versions.map((version) => (
                <option key={version.id} value={version.version_number}>
                  v{version.version_number}
                  {version.version_label ? ` — ${version.version_label}` : ''}
                  {version.state ? ` (${version.state})` : ''}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      <section className="card workspace-processing">
        <ProcessingStatusTracker documentId={doc.id} />
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
        <section className="card workspace-viewer" aria-label="Document viewer">
          <div className="workspace-viewer-head">
            <h2 className="section-title">Original file</h2>
            <ViewerToolbar
              isPdf={isPdf}
              currentPage={currentPage}
              totalPages={totalPages}
              onPageChange={setCurrentPage}
              zoom={zoom}
              onZoomChange={setZoom}
            />
          </div>

          {searchOpen && (
            <div style={{ marginBottom: '0.5rem' }}>
              <DocumentSearchBar
                open={searchOpen}
                query={searchTerm}
                onQueryChange={(query) => {
                  setSearchTerm(query)
                  setMatchIndex(-1)
                }}
                matchCount={matches.length}
                matchIndex={matchIndex}
                onPrev={() => stepMatch(-1)}
                onNext={() => stepMatch(1)}
                onClose={() => {
                  setSearchOpen(false)
                  setSearchTerm('')
                  setMatches([])
                  setMatchIndex(-1)
                }}
              />
            </div>
          )}

          <ViewerBody
            documentId={documentId}
            isPdf={isPdf}
            versionParam={versionParam}
            currentPage={currentPage}
            onPageChange={setCurrentPage}
            onTotalPages={setTotalPages}
            searchTerm={searchOpen || searchTerm ? searchTerm : ''}
            matchIndex={matchIndex}
            onMatchesFound={handleMatchesFound}
            zoom={zoom}
            onTextLayerReady={handleTextLayerReady}
          />
        </section>

        <div className="workspace-side">
          <TocPanel
            items={tocData?.items ?? []}
            hasStructure={tocData?.has_structure ?? false}
            sectionCount={tocData?.section_count ?? 0}
            isLoading={tocLoading}
            conflictSectionIds={conflictSectionIds}
            currentPage={currentPage}
            onSectionClick={(startPage) => {
              setCurrentPage(startPage)
              onJumpToPage(startPage)
            }}
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

// ─── Viewer toolbar (zoom + page status) ──────────────────────────────────────

function ViewerToolbar({
  isPdf,
  currentPage,
  totalPages,
  onPageChange,
  zoom,
  onZoomChange,
}: {
  isPdf: boolean
  currentPage: number
  totalPages: number | null
  onPageChange: (page: number) => void
  zoom: ZoomMode
  onZoomChange: (zoom: ZoomMode) => void
}) {
  if (!isPdf) return null
  return (
    <div className="viewer-toolbar">
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={() => onPageChange(Math.max(1, currentPage - 1))}
        disabled={currentPage <= 1}
        aria-label="Previous page (left arrow)"
      >
        ←
      </button>
      <span className="viewer-page-status" aria-live="polite">
        {currentPage}
        {totalPages ? ` / ${totalPages}` : ''}
      </span>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={() => onPageChange(totalPages ? currentPage + 1 : currentPage)}
        disabled={totalPages != null && currentPage >= totalPages}
        aria-label="Next page (right arrow)"
      >
        →
      </button>
      <select
        aria-label="Zoom"
        value={String(zoom)}
        onChange={(event) => {
          const value = event.target.value
          onZoomChange(
            value === 'fit-width' || value === 'fit-page'
              ? value
              : Number(value),
          )
        }}
      >
        <option value="fit-width">Fit width</option>
        <option value="fit-page">Fit page</option>
        <option value="0.5">50%</option>
        <option value="0.75">75%</option>
        <option value="1">100%</option>
        <option value="1.25">125%</option>
        <option value="1.5">150%</option>
      </select>
    </div>
  )
}

// ─── Viewer body (PDF.js vs fallback) ─────────────────────────────────────────

function ViewerBody({
  documentId,
  isPdf,
  versionParam,
  currentPage,
  onPageChange,
  onTotalPages,
  searchTerm,
  matchIndex,
  onMatchesFound,
  zoom,
  onTextLayerReady,
}: {
  documentId: string | null
  isPdf: boolean
  versionParam: number | null
  currentPage: number
  onPageChange: (page: number) => void
  onTotalPages: (total: number) => void
  searchTerm: string
  matchIndex: number
  onMatchesFound: (matches: SearchMatch[]) => void
  zoom: ZoomMode
  onTextLayerReady: () => void
}) {
  const {
    data: signed,
    isLoading,
    isError,
    refetch,
  } = useSignedUrl(documentId, { version: versionParam ?? undefined })

  if (!isPdf) {
    return (
      <div className="viewer-placeholder">
        Inline preview for this file type arrives with the full workspace.
        Download the original to view it.
      </div>
    )
  }

  if (isLoading) {
    return <div className="viewer-placeholder">Signing download link…</div>
  }
  if (isError || !signed) {
    return (
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
    )
  }

  return (
    <PdfViewer
      url={signed.url}
      page={currentPage}
      onPageChange={onPageChange}
      onTotalPages={onTotalPages}
      searchTerm={searchTerm}
      activeMatchIndex={matchIndex}
      onMatchesFound={onMatchesFound}
      zoom={zoom}
      onTextLayerReady={onTextLayerReady}
    />
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
