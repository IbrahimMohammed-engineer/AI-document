/**
 * PdfViewer — PDF.js canvas + text layer (Phase 15 §6.4.1).
 *
 * Replaces the Phase 5 <iframe> groundwork:
 *  - Renders ONE page at a time on a canvas (memory-safe for large docs)
 *  - Text layer: absolutely-positioned spans over the canvas → selectable,
 *    screen-reader-accessible, and the mount for <mark> search highlights
 *  - Zoom: fit-width / fit-page / 50–150%
 *  - Keyboard: ←/→ page turn, PageUp/PageDown (bound on the container)
 *  - In-document search state comes from DocumentSearchBar via props —
 *    matches are highlighted in the text layer; this component exposes
 *    `onMatchesFound` so the bar can show "N / M"
 *  - Deep link: on mount, jumps to `page` then highlights `highlightText`
 *
 * The PDF URL is the short-lived signed download URL.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'

// Phase 15 §6.4.1 — configure the worker here (inside the lazy route chunk)
// so pdfjs-dist never enters the initial bundle. Vite emits the worker file
// via its URL asset pipeline.
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

export type ZoomMode = 'fit-width' | 'fit-page' | number

export interface SearchMatch {
  page: number
  /** Character offset of the match inside the page's text content. */
  start: number
  length: number
  /** Snippet shown by the search bar. */
  context: string
}

interface PdfViewerProps {
  url: string
  page: number
  onPageChange: (page: number) => void
  /** Fired when the page's text layer has been built. */
  onTextLayerReady?: () => void
  /** 1-indexed total pages once the document loads (reported via callback). */
  onTotalPages?: (total: number) => void
  /** Current search term (matches are highlighted across text layers). */
  searchTerm?: string
  /** Which match index (0-based, document-wide) to focus. */
  activeMatchIndex?: number
  onMatchesFound?: (matches: SearchMatch[]) => void
  zoom: ZoomMode
}

const RENDER_SCALE_PADDING = 2 // device pixels for crisp text

export function PdfViewer({
  url,
  page,
  onPageChange,
  onTextLayerReady,
  onTotalPages,
  searchTerm = '',
  activeMatchIndex = -1,
  onMatchesFound,
  zoom,
}: PdfViewerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const textLayerRef = useRef<HTMLDivElement | null>(null)
  const renderTaskRef = useRef<{ cancel: () => void; promise: Promise<void> } | null>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  const pageTextRef = useRef<Map<number, string>>(new Map())

  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [totalPages, setTotalPages] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [renderedPage, setRenderedPage] = useState(0)
  const layerId = useId()

  // ── Load the document ─────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError(null)
    pageTextRef.current.clear()

    pdfjsLib
      .getDocument({ url })
      .promise.then((doc) => {
        if (cancelled) {
          void doc.destroy()
          return
        }
        docRef.current = doc
        setTotalPages(doc.numPages)
        onTotalPages?.(doc.numPages)
        setStatus('ready')
      })
      .catch((err: Error) => {
        if (cancelled) return
        setStatus('error')
        setError(err.message || 'Could not load the PDF.')
      })

    return () => {
      cancelled = true
      void docRef.current?.destroy()
      docRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url])

  // ── Render the current page ───────────────────────────────────────────────
  const renderPage = useCallback(
    async (pageNumber: number) => {
      const doc = docRef.current
      const canvas = canvasRef.current
      const textLayerEl = textLayerRef.current
      if (!doc || !canvas || !textLayerEl) return

      const pdfPage: PDFPageProxy = await doc.getPage(pageNumber)
      const container = containerRef.current
      const containerWidth = container?.clientWidth ?? 800
      const containerHeight = container?.clientHeight ?? 900

      const baseViewport = pdfPage.getViewport({ scale: 1 })
      let scale: number
      if (zoom === 'fit-width') {
        scale = containerWidth / baseViewport.width
      } else if (zoom === 'fit-page') {
        scale = Math.min(
          containerWidth / baseViewport.width,
          containerHeight / baseViewport.height,
        )
      } else {
        scale = zoom
      }

      // Cancel any in-flight render before starting a new one
      renderTaskRef.current?.cancel()

      const viewport = pdfPage.getViewport({
        scale: scale * RENDER_SCALE_PADDING,
      })
      canvas.width = Math.floor(viewport.width)
      canvas.height = Math.floor(viewport.height)
      canvas.style.width = `${Math.floor(viewport.width / RENDER_SCALE_PADDING)}px`
      canvas.style.height = `${Math.floor(viewport.height / RENDER_SCALE_PADDING)}px`

      const renderTask = pdfPage.render({
        canvasContext: canvas.getContext('2d') as CanvasRenderingContext2D,
        viewport,
      })
      renderTaskRef.current = renderTask
      try {
        await renderTask.promise
      } catch (err) {
        const name = (err as { name?: string }).name
        if (name === 'RenderingCancelledException') return
        throw err
      }

      // ── Text layer ────────────────────────────────────────────────────────
      const textContent = await pdfPage.getTextContent()
      const cssViewport = pdfPage.getViewport({ scale })
      textLayerEl.innerHTML = ''
      textLayerEl.style.width = `${Math.floor(cssViewport.width)}px`
      textLayerEl.style.height = `${Math.floor(cssViewport.height)}px`

      let fullText = ''
      const spans: { node: HTMLSpanElement; start: number }[] = []

      for (const item of textContent.items) {
        if (!('str' in item)) continue
        const transform = (pdfjsLib.Util as typeof pdfjsLib.Util).transform(
          (pdfjsLib.Util as typeof pdfjsLib.Util).transform(
            cssViewport.transform,
            item.transform as unknown as DOMMatrix2DInit,
          ),
          [1, 0, 0, 1, 0, 0],
        )
        const fontHeight = Math.hypot(transform[2], transform[3])
        const left = transform[4]
        const top = transform[5] - fontHeight

        const span = document.createElement('span')
        span.textContent = item.str
        span.style.left = `${left}px`
        span.style.top = `${top}px`
        span.style.fontSize = `${fontHeight}px`
        span.style.position = 'absolute'
        span.style.whiteSpace = 'pre'
        span.style.transformOrigin = '0% 0%'
        textLayerEl.appendChild(span)

        spans.push({ node: span, start: fullText.length })
        fullText += item.str
        if ('hasEOL' in item && item.hasEOL) fullText += '\n'
      }

      pageTextRef.current.set(pageNumber, fullText)
      spansRef.current = spans
      setRenderedPage(pageNumber)
      onTextLayerReady?.()
    },
    [zoom, onTextLayerReady],
  )

  // Ref for the per-page span map (populated by renderPage, read by highlighting)
  const spansRef = useRef<{ node: HTMLSpanElement; start: number }[]>([])

  useEffect(() => {
    if (status !== 'ready' || !totalPages) return
    const clamped = Math.min(Math.max(page, 1), totalPages)
    if (clamped !== page) {
      onPageChange(clamped)
      return
    }
    let cancelled = false
    renderPage(clamped).catch((err: Error) => {
      if (!cancelled) {
        setStatus('error')
        setError(err.message || 'Could not render the page.')
      }
    })
    return () => {
      cancelled = true
    }
  }, [status, totalPages, page, renderPage, onPageChange])

  // ── Search highlighting across the rendered text layer ───────────────────
  useEffect(() => {
    const textLayerEl = textLayerRef.current
    if (!textLayerEl) return

    // Clear previous marks
    textLayerEl.querySelectorAll('mark.pdf-search-mark').forEach((mark) => {
      const parent = mark.parentNode
      if (parent) {
        parent.replaceChild(document.createTextNode(mark.textContent ?? ''), mark)
        parent.normalize()
      }
    })
    const term = searchTerm.trim().toLowerCase()
    if (!term || !searchTerm.trim()) {
      onMatchesFound?.([])
      return
    }

    // Build document-wide matches from cached page texts
    const matches: SearchMatch[] = []
    for (const [pageNumber, text] of pageTextRef.current) {
      const haystack = text.toLowerCase()
      let at = haystack.indexOf(term)
      while (at !== -1) {
        matches.push({
          page: pageNumber,
          start: at,
          length: term.length,
          context: text.slice(Math.max(0, at - 30), at + term.length + 30),
        })
        at = haystack.indexOf(term, at + term.length)
      }
    }
    matches.sort((a, b) => a.page - b.page || a.start - b.start)
    onMatchesFound?.(matches)

    // Highlight within the CURRENTLY RENDERED page's spans
    const pageText = pageTextRef.current.get(renderedPage)
    if (!pageText) return
    const pageMatches = matches.filter((match) => match.page === renderedPage)
    if (pageMatches.length === 0) return

    for (const match of pageMatches) {
      for (const span of spansRef.current) {
        const spanEnd = span.start + (span.node.textContent?.length ?? 0)
        if (match.start < spanEnd && match.start + match.length > span.start) {
          highlightInSpan(span, match, span.node.parentNode as HTMLElement)
        }
      }
    }

    function highlightInSpan(
      span: { node: HTMLSpanElement; start: number },
      match: SearchMatch,
      parent: HTMLElement | null,
    ) {
      if (!parent) return
      const text = span.node.textContent ?? ''
      const localStart = Math.max(0, match.start - span.start)
      const localEnd = Math.min(text.length, match.start + match.length - span.start)
      if (localStart >= localEnd) return

      const mark = document.createElement('mark')
      mark.className = 'pdf-search-mark'
      mark.textContent = text.slice(localStart, localEnd)
      const after = text.slice(localEnd)
      span.node.textContent = text.slice(0, localStart)
      parent.insertBefore(mark, span.node.nextSibling)
      if (after) {
        parent.insertBefore(document.createTextNode(after), mark.nextSibling)
      }
      mark.dataset.matchActive = 'false'
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchTerm, renderedPage, onMatchesFound, activeMatchIndex === -1])

  // Active-match emphasis + scroll into view
  useEffect(() => {
    const textLayerEl = textLayerRef.current
    if (!textLayerEl || activeMatchIndex < 0) return
    const marks = Array.from(
      textLayerEl.querySelectorAll<HTMLElement>('mark.pdf-search-mark'),
    )
    marks.forEach((mark) => (mark.dataset.matchActive = 'false'))
    if (marks.length > 0) {
      // The active match may live on another page — highlight what renders
      marks[0].dataset.matchActive = 'true'
      marks[0].scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [activeMatchIndex, renderedPage])

  // ── Keyboard navigation ───────────────────────────────────────────────────
  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (!totalPages) return
    if (event.key === 'ArrowRight' && page < totalPages) {
      event.preventDefault()
      onPageChange(page + 1)
    } else if (event.key === 'ArrowLeft' && page > 1) {
      event.preventDefault()
      onPageChange(page - 1)
    } else if (event.key === 'PageDown' && page < totalPages) {
      event.preventDefault()
      onPageChange(page + 1)
    } else if (event.key === 'PageUp' && page > 1) {
      event.preventDefault()
      onPageChange(page - 1)
    }
  }

  const pageText = pageTextRef.current.get(renderedPage) ?? ''

  if (status === 'loading') {
    return (
      <div className="viewer-placeholder" aria-busy="true">
        <span className="spinner spinner-sm" /> Loading PDF…
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className="viewer-placeholder viewer-placeholder--error">
        {error ?? 'Could not load the PDF.'}
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className="pdf-viewer"
      tabIndex={0}
      role="region"
      aria-label={
        totalPages
          ? `PDF page ${page} of ${totalPages}`
          : 'PDF document'
      }
      onKeyDown={handleKeyDown}
    >
      <div className="pdf-page-surface">
        <canvas
          ref={canvasRef}
          role="img"
          aria-label={
            totalPages
              ? `Page ${renderedPage} of ${totalPages}`
              : 'PDF page'
          }
        />
        {/* Text layer: real content for selection + screen readers; visually
            transparent ink positioned over the canvas. */}
        <div
          ref={textLayerRef}
          id={`pdf-text-layer-${layerId}`}
          className="pdf-text-layer"
          aria-hidden="false"
        />
      </div>
      <span className="sr-only">{pageText.slice(0, 4000)}</span>
    </div>
  )
}
