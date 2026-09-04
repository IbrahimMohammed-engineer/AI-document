/**
 * TocPanel — the workspace table-of-contents panel (FE §6.5, Phase 6 slice).
 *
 * Renders the real `document_sections` tree produced by heuristic structure
 * detection. Per the frontend state matrix (FE §6.5 / FE §17):
 *  - Loading      → 5-line TOC skeleton
 *  - Empty        → "No structure detected for this document" with the
 *                   page-based-navigation fallback note (an explicitly
 *                   supported backend state, Backend §20)
 *  - Ready        → nested section tree; nodes show the section number and
 *                   the page span; click selects the section (jumping into
 *                   the viewer deepens with the full workspace, Phase 15)
 */

import { useState, type CSSProperties, type ReactNode } from 'react'

import type { TocNode } from '@/lib/api/documents'

export function TocPanel({
  items,
  hasStructure,
  sectionCount,
  isLoading,
  conflictSectionIds,
}: {
  items: TocNode[]
  hasStructure: boolean
  sectionCount: number
  isLoading: boolean
  /** Phase 13 — section labels with an unresolved conflict (FE §6.5 `[•]`). */
  conflictSectionIds?: Set<string>
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string | null>(null)

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
      const pageSpan =
        node.end_page != null && node.end_page !== node.start_page
          ? `p. ${node.start_page}–${node.end_page}`
          : `p. ${node.start_page}`
      return (
        <li
          key={node.id}
          className="toc-item"
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
              onClick={() => {
                // Page-jump deepens with the full viewer (Phase 15); for
                // now the section's page is in the title and the selection
                // is announced to assistive tech via aria-current.
                setSelected(node.id)
              }}
              aria-current={selected === node.id ? 'true' : undefined}
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
