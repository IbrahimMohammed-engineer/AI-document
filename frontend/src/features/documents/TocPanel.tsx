/**
 * TocPanel — the workspace table-of-contents panel (FE §6.5, Phase 6 slice).
 *
 * Phase 15 additions (§6.4.3):
 *  - Scrollspy: the section whose `start_page`–`end_page` range contains the
 *    viewer's current page is highlighted with `aria-current="true"` and the
 *    active style as the PDF page changes.
 *  - `onSectionClick(startPage)` fires on section click → the viewer jumps.
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import type { TocNode } from '@/lib/api/documents'

export function TocPanel({
  items,
  hasStructure,
  sectionCount,
  isLoading,
  conflictSectionIds,
  currentPage,
  onSectionClick,
}: {
  items: TocNode[]
  hasStructure: boolean
  sectionCount: number
  isLoading: boolean
  /** Phase 13 — section labels with an unresolved conflict (FE §6.5 `[•]`). */
  conflictSectionIds?: Set<string>
  /** Phase 15 scrollspy — the page currently shown in the viewer. */
  currentPage?: number
  /** Phase 15 — jump the viewer to a section's first page. */
  onSectionClick?: (startPage: number) => void
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  /** Deepest node containing the current page (scrollspy winner). */
  const activeNodeId = (() => {
    if (currentPage == null) return null
    let best: { id: string; level: number } | null = null
    const visit = (node: TocNode) => {
      const inRange =
        currentPage >= node.start_page &&
        (node.end_page == null || currentPage <= node.end_page)
      if (inRange && (!best || node.level >= best.level)) {
        best = { id: node.id, level: node.level }
      }
      node.children.forEach(visit)
    }
    items.forEach(visit)
    return best ? (best as { id: string }).id : null
  })()

  if (isLoading) return <TocSkeleton />

  if (!hasStructure || items.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-state-title">No structure detected</div>
        <p className="empty-state-description">
          No headings were detected in this document — page-based navigation
          is available instead.
        </p>
      </div>
    )
  }

  const hasConflictMarker = conflictSectionIds && conflictSectionIds.size > 0

  const nodeHasConflict = (node: TocNode): boolean => {
    if (!hasConflictMarker) return false
    const labels = [
      node.section_number ? `${node.section_number} ${node.title}` : null,
      node.title,
      node.section_number,
    ].filter(Boolean) as string[]
    return labels.some((label) => conflictSectionIds.has(label))
  }

  const toggle = (nodeId: string) => {
    setCollapsed((previous) => {
      const next = new Set(previous)
      if (next.has(nodeId)) next.delete(nodeId)
      else next.add(nodeId)
      return next
    })
  }

  const renderNodes = (nodes: TocNode[], depth = 0): ReactNode =>
    nodes.map((node) => {
      const isCollapsed = collapsed.has(node.id)
      const hasChildren = node.children.length > 0
      const isActive = activeNodeId === node.id
      const pageSpan =
        node.end_page != null && node.end_page !== node.start_page
          ? `p. ${node.start_page}–${node.end_page}`
          : `p. ${node.start_page}`
      return (
        <li
          key={node.id}
          className={`toc-item${isActive ? ' toc-item--active' : ''}`}
          style={{ '--toc-depth': depth } as CSSProperties}
        >
          <div className="toc-row">
            {hasChildren ? (
              <button
                type="button"
                className="toc-toggle"
                aria-expanded={!isCollapsed}
                aria-label={isCollapsed ? 'Expand section' : 'Collapse section'}
                onClick={() => toggle(node.id)}
              >
                {isCollapsed ? '▸' : '▾'}
              </button>
            ) : (
              <span className="toc-toggle toc-toggle--leaf" aria-hidden="true">
                •
              </span>
            )}
            <button
              type="button"
              className="toc-link"
              title={`Go to page ${node.start_page}`}
              onClick={() => onSectionClick?.(node.start_page)}
              aria-current={isActive ? 'true' : undefined}
            >
              {node.section_number && (
                <span className="toc-number">{node.section_number}</span>
              )}
              <span className="toc-title">{node.title}</span>
            </button>
            <span className="toc-pages">
              {pageSpan}
              {nodeHasConflict(node) && (
                <span
                  className="toc-conflict-marker"
                  role="img"
                  aria-label="Unresolved conflict in this section"
                  title="Unresolved conflict in this section"
                >
                  •
                </span>
              )}
            </span>
          </div>
          {hasChildren && !isCollapsed && (
            <ul className="toc-list toc-list--nested">
              {renderNodes(node.children, depth + 1)}
            </ul>
          )}
        </li>
      )
    })

  return (
    <nav className="toc-panel" aria-label="Table of contents">
      <div className="workspace-toc-head">
        <h2 className="section-title">Contents</h2>
        <span className="workspace-toc-count">
          {sectionCount} section{sectionCount === 1 ? '' : 's'}
        </span>
      </div>
      <ul className="toc-list">{renderNodes(items)}</ul>
    </nav>
  )
}

function TocSkeleton() {
  return (
    <div className="toc-skeleton" aria-hidden="true">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          className="toc-skeleton-line"
          style={{ width: `${88 - i * 11}%` }}
        />
      ))}
    </div>
  )
}
