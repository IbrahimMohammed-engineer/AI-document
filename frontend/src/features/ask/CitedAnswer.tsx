/**
 * CitedAnswer (FE §6.7/§12) — renders the validated answer text with inline
 * citation markers ([1], [1][2], [SOURCE 1]) replaced by CitationBadge
 * components.  Canonical interaction contract implemented ONCE here and
 * reused by every AI answer surface from Phase 10 onward.
 *
 * Markers the citations array cannot resolve render as the muted
 * "source unavailable" badge — badges are never fabricated client-side.
 */
import type { AskCitation } from '@/lib/api/ask'
import { CitationBadge } from './CitationBadge'

const MARKER_SPLIT = /(\[(?:SOURCE\s*)?\d{1,2}\])/g
const MARKER_TEST = /^\[(?:SOURCE\s*)?\d{1,2}\]$/
const MARKER_INDEX = /\d{1,2}/

export function CitedAnswer({
  text,
  citations,
}: {
  text: string
  citations: AskCitation[]
}) {
  if (!text) return null

  const byIndex = new Map<number, AskCitation>(
    citations.map((c) => [c.index, c]),
  )

  const parts = text.split(MARKER_SPLIT)
  return (
    <div className="ask-answer">
      {parts.map((part, i) => {
        if (MARKER_TEST.test(part)) {
          const index = Number.parseInt(MARKER_INDEX.exec(part)?.[0] ?? '', 10)
          if (!Number.isNaN(index)) {
            return (
              <CitationBadge
                key={`${i}-${index}`}
                citation={byIndex.get(index) ?? null}
              />
            )
          }
        }
        return <span key={i}>{part}</span>
      })}
    </div>
  )
}
