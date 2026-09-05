/**
 * Breadcrumbs (Phase 15 §6.1.3).
 *
 * Renders `<nav aria-label="Breadcrumb"><ol>…</ol></nav>` from the matched
 * route chain. Each route in App.tsx declares `handle: { crumb: 'Label' }`
 * (string or function of params for dynamic labels). Depth-1 routes render
 * nothing.
 */

import { Link, useMatches } from 'react-router-dom'

interface RouteHandle {
  crumb?: string | ((params: Record<string, string | undefined>) => string)
}

export function Breadcrumbs() {
  const matches = useMatches()
  const crumbs = matches
    .filter((match) => Boolean((match.handle as RouteHandle | undefined)?.crumb))
    .map((match) => {
      const crumb = (match.handle as RouteHandle).crumb
      return {
        label: typeof crumb === 'function' ? crumb(match.params) : crumb,
        pathname: match.pathname,
      }
    })

  if (crumbs.length <= 1) return null

  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      <ol className="breadcrumbs-list">
        {crumbs.map((crumb, index) => {
          const isLast = index === crumbs.length - 1
          return (
            <li key={crumb.pathname} className="breadcrumbs-item">
              {isLast ? (
                <span aria-current="page">{crumb.label}</span>
              ) : (
                <Link to={crumb.pathname}>{crumb.label}</Link>
              )}
              {!isLast && (
                <span className="breadcrumbs-separator" aria-hidden="true">
                  /
                </span>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
