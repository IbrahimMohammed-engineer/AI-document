/**
 * ComparisonPage — full comparison workflow (Phase 12).
 *
 * Route: /app/compare  (no comparisonId) → version picker only
 * Route: /app/compare/:comparisonId       → live comparison detail
 *
 * Flow:
 *   1. User picks two document versions via the VersionPicker.
 *   2. POST /documents/compare creates/reuses a comparison (202).
 *   3. Page navigates to /app/compare/:id and starts polling.
 *   4. When COMPLETED: summary cards, filterable change list, AI narration.
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import './comparison.css'
import {
  useComparison,
  useComparisonChanges,
  useComparisonNarration,
  useDocumentVersions,
  useInitiateComparison,
} from '@/hooks/queries/useComparisons'
import { useQuery } from '@tanstack/react-query'
import { listDocumentsApi, type DocumentListResponse } from '@/lib/api/documents'
import { useAuthStore } from '@/store/authStore'
import type { ChangeSeverity } from '@/lib/api/comparison'

/** Minimal document list hook for the version picker document selector. */
function useDocumentList() {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<DocumentListResponse>({
    queryKey: ['documents', 'list', 'picker'],
    enabled: Boolean(accessToken),
    queryFn: () => listDocumentsApi({ limit: 100 }),
    staleTime: 30_000,
  })
}


// ── Helpers ───────────────────────────────────────────────────────────────────

function SeverityBadge({ severity }: { severity: string }) {
  const cls = `change-badge badge-${severity.toLowerCase()}`
  const icons: Record<string, string> = { MAJOR: '⬆', MODERATE: '→', MINOR: '↓' }
  return <span className={cls}>{icons[severity] ?? ''} {severity}</span>
}

function TypeBadge({ type }: { type: string }) {
  const cls = `change-badge badge-${type.toLowerCase()}`
  const icons: Record<string, string> = { ADDED: '+', REMOVED: '−', MODIFIED: '≠' }
  return <span className={cls}>{icons[type] ?? ''} {type}</span>
}

// ── Status banner ─────────────────────────────────────────────────────────────

function ComparisonStatusBanner({
  status,
  errorMessage,
}: {
  status: string
  errorMessage?: string | null
}) {
  const configs = {
    PENDING: {
      cls: 'comparison-status--pending',
      icon: null,
      spinner: true,
      text: 'Comparison queued…',
      sub: 'Your comparison is waiting to be processed.',
    },
    PROCESSING: {
      cls: 'comparison-status--processing',
      icon: null,
      spinner: true,
      text: 'Comparing documents…',
      sub: 'Analysing sections, detecting changes, and classifying severity.',
    },
    COMPLETED: {
      cls: 'comparison-status--completed',
      icon: '✓',
      spinner: false,
      text: 'Comparison complete',
      sub: 'All changes have been identified and classified.',
    },
    FAILED: {
      cls: 'comparison-status--failed',
      icon: '✕',
      spinner: false,
      text: 'Comparison failed',
      sub: errorMessage ?? 'An unexpected error occurred.',
    },
  }

  const cfg = configs[status as keyof typeof configs] ?? configs.PENDING

  return (
    <div className={`comparison-status ${cfg.cls}`}>
      {cfg.spinner && <div className="comparison-status__spinner" aria-label="Loading" />}
      {cfg.icon && <span className="comparison-status__icon">{cfg.icon}</span>}
      <div>
        <div className="comparison-status__text">{cfg.text}</div>
        <div className="comparison-status__sub">{cfg.sub}</div>
      </div>
    </div>
  )
}

// ── Summary cards ─────────────────────────────────────────────────────────────

function ComparisonSummaryCards({
  summary,
  activeFilter,
  onFilterChange,
}: {
  summary: { total: number; major: number; moderate: number; minor: number }
  activeFilter: ChangeSeverity | null
  onFilterChange: (f: ChangeSeverity | null) => void
}) {
  const cards = [
    { key: null,       label: 'Total',    count: summary.total,    cls: 'comparison-summary__card--total' },
    { key: 'MAJOR',    label: 'Major',    count: summary.major,    cls: 'comparison-summary__card--major' },
    { key: 'MODERATE', label: 'Moderate', count: summary.moderate, cls: 'comparison-summary__card--moderate' },
    { key: 'MINOR',    label: 'Minor',    count: summary.minor,    cls: 'comparison-summary__card--minor' },
  ] as const

  return (
    <div className="comparison-summary">
      {cards.map(({ key, label, count, cls }) => (
        <button
          key={label}
          type="button"
          className={[
            'comparison-summary__card',
            cls,
            activeFilter === key ? 'comparison-summary__card--active' : '',
          ].join(' ')}
          onClick={() => onFilterChange(activeFilter === key ? null : key)}
          aria-pressed={activeFilter === key}
          id={`comparison-filter-${label.toLowerCase()}`}
        >
          <div className="comparison-summary__count">{count}</div>
          <div className="comparison-summary__label">{label}</div>
        </button>
      ))}
    </div>
  )
}

// ── Change item ───────────────────────────────────────────────────────────────

function SourceButton({
  label,
  documentId,
  chunkId,
  pageNumber,
  quote,
}: {
  label: string
  documentId: string | null
  chunkId: string | null
  pageNumber?: number
  quote: string | null
}) {
  if (!documentId || !chunkId) return null
  const params = new URLSearchParams()
  if (pageNumber != null) params.set('page', String(pageNumber))
  if (quote) params.set('q', quote.slice(0, 120))
  params.set('chunk', chunkId)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return (
    <Link
      to={`/app/documents/${documentId}${suffix}`}
      className="btn btn-secondary btn-xs comparison-change-item__source-btn"
      aria-label={`${label} — open the source in the document workspace`}
    >
      View source
    </Link>
  )
}

function ChangeItem({
  change,
  documentAId,
  documentBId,
}: {
  change: {
    id: string
    change_type: string
    severity: string
    section: string | null
    old_chunk_id?: string | null
    new_chunk_id?: string | null
    old_text: string | null
    new_text: string | null
    old_source?: { document_id: string; page_number: number } | null
    new_source?: { document_id: string; page_number: number } | null
  }
  documentAId?: string | null
  documentBId?: string | null
}) {
  const oldDocId = change.old_source?.document_id ?? documentAId ?? null
  const newDocId = change.new_source?.document_id ?? documentBId ?? null

  return (
    <div className="comparison-change-item">
      <div className="comparison-change-item__badges">
        <SeverityBadge severity={change.severity} />
        <TypeBadge type={change.change_type} />
      </div>
      <div className="comparison-change-item__body">
        <div className="comparison-change-item__section">
          {change.section ?? 'Unknown section'}
        </div>

        {(change.old_text || change.new_text) && (
          <div className="comparison-change-item__diff">
            {change.old_text && (
              <div className="comparison-change-item__side comparison-change-item__side--old">
                <div className="comparison-change-item__side-label">
                  Before
                  <SourceButton
                    label="View the old source"
                    documentId={oldDocId}
                    chunkId={change.old_chunk_id ?? null}
                    pageNumber={change.old_source?.page_number}
                    quote={change.old_text}
                  />
                </div>
                {change.old_text}
              </div>
            )}
            {change.new_text && (
              <div className="comparison-change-item__side comparison-change-item__side--new">
                <div className="comparison-change-item__side-label">
                  After
                  <SourceButton
                    label="View the new source"
                    documentId={newDocId}
                    chunkId={change.new_chunk_id ?? null}
                    pageNumber={change.new_source?.page_number}
                    quote={change.new_text}
                  />
                </div>
                {change.new_text}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Change list ───────────────────────────────────────────────────────────────

function ComparisonChangeList({
  comparisonId,
  activeFilter,
  onFilterChange,
  anchorSection,
  documentAId,
  documentBId,
}: {
  comparisonId: string
  activeFilter: ChangeSeverity | null
  onFilterChange: (f: ChangeSeverity | null) => void
  /** Phase 13 — section anchor from a conflict's "Compare Sources" (§22). */
  anchorSection?: string | null
  /** Phase 15 — parent doc IDs for the "View source" buttons. */
  documentAId?: string | null
  documentBId?: string | null
}) {
  const { data, isLoading } = useComparisonChanges(comparisonId, activeFilter ?? undefined)
  const anchored = useRef(false)

  // Section anchoring: scroll to + highlight the change matching the
  // conflicting section once the change list arrives (one-shot).
  useEffect(() => {
    if (anchored.current || !anchorSection || !data?.items.length) return
    const target = document.getElementById('comparison-anchor-target')
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      anchored.current = true
    }
  }, [anchorSection, data?.items.length])

  const filters: Array<{ label: string; value: ChangeSeverity | null }> = [
    { label: 'All', value: null },
    { label: 'Major', value: 'MAJOR' },
    { label: 'Moderate', value: 'MODERATE' },
    { label: 'Minor', value: 'MINOR' },
  ]

  return (
    <div className="comparison-changes">
      <div className="comparison-changes__header">
        <span className="comparison-changes__title">
          {data ? `${data.total} change${data.total !== 1 ? 's' : ''}` : 'Changes'}
          {activeFilter && ` · filtered by ${activeFilter}`}
        </span>
        <div className="comparison-changes__filter-group" role="group" aria-label="Filter by severity">
          {filters.map(({ label, value }) => (
            <button
              key={label}
              type="button"
              className={[
                'comparison-changes__filter-btn',
                activeFilter === value ? 'comparison-changes__filter-btn--active' : '',
              ].join(' ')}
              onClick={() => onFilterChange(value)}
              id={`changes-filter-${label.toLowerCase()}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {isLoading && (
        <div className="comparison-empty">
          <div className="comparison-empty__icon">⌛</div>
          <div className="comparison-empty__title">Loading changes…</div>
        </div>
      )}

      {!isLoading && !data?.items.length && (
        <div className="comparison-empty">
          <div className="comparison-empty__icon">✓</div>
          <div className="comparison-empty__title">No changes found</div>
          <div className="comparison-empty__desc">
            {activeFilter
              ? `No ${activeFilter.toLowerCase()} changes detected.`
              : 'The two versions appear to be identical.'}
          </div>
        </div>
      )}

      {data?.items.map((change) => {
        const isAnchor =
          Boolean(anchorSection) && !anchored.current
          && change.section != null
          && (change.section === anchorSection
            || anchorSection.includes(change.section)
            || change.section.includes(anchorSection))
        return (
          <div
            key={change.id}
            id={isAnchor && !anchored.current ? 'comparison-anchor-target' : undefined}
            className={isAnchor ? 'comparison-change-item comparison-change-item--anchor' : undefined}
          >
            <ChangeItem
              change={change}
              documentAId={documentAId}
              documentBId={documentBId}
            />
          </div>
        )
      })}
    </div>
  )
}

// ── Narration panel ───────────────────────────────────────────────────────────

function ComparisonNarrationPanel({ comparisonId }: { comparisonId: string }) {
  const { data, isLoading } = useComparisonNarration(comparisonId, true)

  return (
    <div className="comparison-narration">
      <div className="comparison-narration__header">
        <div className="comparison-narration__icon">✦</div>
        <span className="comparison-narration__title">AI Summary</span>
        {data && !data.llm_narrated && (
          <span className="comparison-narration__badge">plain text fallback</span>
        )}
      </div>

      {isLoading && (
        <div className="comparison-narration__loading">
          <div className="comparison-status__spinner" />
          Generating summary…
        </div>
      )}

      {data && (
        <div className="comparison-narration__body">{data.narration}</div>
      )}
    </div>
  )
}

// ── Version picker ────────────────────────────────────────────────────────────

function VersionPicker({ onCompare, prefillDocA, prefillDocB }: {
  onCompare: (a: string, b: string) => void
  prefillDocA?: string
  prefillDocB?: string
}) {
  // Use first 2 documents in organization as default — user selects versions
  const [docAId, setDocAId] = useState(prefillDocA ?? '')
  const [docBId, setDocBId] = useState(prefillDocB ?? '')
  const [versionAId, setVersionAId] = useState('')
  const [versionBId, setVersionBId] = useState('')
  const [error, setError] = useState<string | null>(null)

  const { data: docsData } = useDocumentList()
  const { data: versionsA } = useDocumentVersions(docAId || null)
  const { data: versionsB } = useDocumentVersions(docBId || null)

  const mutation = useInitiateComparison()
  const navigate = useNavigate()

  async function handleCompare() {
    setError(null)
    if (!versionAId || !versionBId) {
      setError('Please select both versions to compare.')
      return
    }
    if (versionAId === versionBId) {
      setError('Please select two different versions.')
      return
    }
    try {
      const result = await mutation.mutateAsync({
        document_a_version_id: versionAId,
        document_b_version_id: versionBId,
      })
      onCompare(result.id, result.id)
      navigate(`/app/compare/${result.id}`)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to start comparison.'
      setError(msg)
    }
  }

  const docs = docsData?.items ?? []

  function VersionSelect({
    docId,
    onDocChange,
    versionId,
    onVersionChange,
    versionsData,
    side,
  }: {
    docId: string
    onDocChange: (id: string) => void
    versionId: string
    onVersionChange: (id: string) => void
    versionsData: typeof versionsA
    side: 'A' | 'B'
  }) {
    return (
      <div>
        <div className="comparison-picker__side-label">Version {side}</div>
        <select
          className="comparison-picker__select"
          value={docId}
          onChange={(e) => { onDocChange(e.target.value); onVersionChange('') }}
          id={`compare-doc-${side.toLowerCase()}`}
          style={{ marginBottom: 'var(--space-2)' }}
        >
          <option value="">Select document…</option>
          {docs.map((d) => (
            <option key={d.id} value={d.id}>{d.name}</option>
          ))}
        </select>

        <select
          className="comparison-picker__select"
          value={versionId}
          onChange={(e) => onVersionChange(e.target.value)}
          disabled={!docId || !versionsData?.items.length}
          id={`compare-version-${side.toLowerCase()}`}
        >
          <option value="">Select version…</option>
          {versionsData?.items
            .filter((v) => v.status === 'READY')
            .map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version_number}
                {v.version_label ? ` — ${v.version_label}` : ''}
                {v.effective_date ? ` (${v.effective_date})` : ''}
                {v.state === 'CURRENT' ? ' · Current' : ''}
              </option>
            ))}
        </select>
      </div>
    )
  }

  return (
    <div className="comparison-picker">
      <div className="comparison-picker__title">Compare Document Versions</div>
      <div className="comparison-picker__desc">
        Select any two READY versions to detect what changed between them.
      </div>

      <div className="comparison-picker__grid">
        <VersionSelect
          docId={docAId}
          onDocChange={setDocAId}
          versionId={versionAId}
          onVersionChange={setVersionAId}
          versionsData={versionsA}
          side="A"
        />

        <div className="comparison-picker__arrow">→</div>

        <VersionSelect
          docId={docBId}
          onDocChange={setDocBId}
          versionId={versionBId}
          onVersionChange={setVersionBId}
          versionsData={versionsB}
          side="B"
        />
      </div>

      {error && <div className="comparison-picker__error">{error}</div>}

      <div className="comparison-picker__actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleCompare}
          disabled={mutation.isPending || !versionAId || !versionBId}
          id="compare-submit-btn"
        >
          {mutation.isPending ? 'Starting…' : 'Compare Versions'}
        </button>
      </div>
    </div>
  )
}

// ── Comparison detail (poll + results) ────────────────────────────────────────

function ComparisonDetail({ comparisonId }: { comparisonId: string }) {
  const { data: comparison } = useComparison(comparisonId)
  const [activeFilter, setActiveFilter] = useState<ChangeSeverity | null>(null)
  const [searchParams] = useSearchParams()
  const anchorSection = searchParams.get('section')
  if (!comparison) {
    return (
      <div className="comparison-status comparison-status--pending">
        <div className="comparison-status__spinner" />
        <div className="comparison-status__text">Loading comparison…</div>
      </div>
    )
  }

  const isCompleted = comparison.status === 'COMPLETED'
  const summary = comparison.summary

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
      <ComparisonStatusBanner
        status={comparison.status}
        errorMessage={comparison.error_message}
      />

      {summary && comparison.summary?.alignment_degraded && (
        <div className="comparison-degraded-warning">
          <span className="comparison-degraded-warning__icon">⚠</span>
          <span>
            Section alignment was degraded — sections could not be matched by number or title.
            The comparison fell back to a whole-document diff, which may be less granular.
          </span>
        </div>
      )}

      {isCompleted && summary && (
        <>
          <ComparisonSummaryCards
            summary={summary}
            activeFilter={activeFilter}
            onFilterChange={setActiveFilter}
          />
          <ComparisonNarrationPanel comparisonId={comparisonId} />
          <ComparisonChangeList
            comparisonId={comparisonId}
            activeFilter={activeFilter}
            onFilterChange={setActiveFilter}
            anchorSection={anchorSection}
            documentAId={comparison.document_a_id}
            documentBId={comparison.document_b_id}
          />
        </>
      )}
    </div>
  )
}

// ── Page root ─────────────────────────────────────────────────────────────────

export function ComparisonPage() {
  const { comparisonId } = useParams<{ comparisonId?: string }>()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const mutation = useInitiateComparison()
  const autoStarted = useRef(false)

  // Phase 13 (§22): "Compare Sources" from a conflict deep-links here with
  // the pair pre-filled (versionA/versionB) — auto-create/reuse the
  // comparison and jump straight to the results, section-anchored.
  const prefillVersionA = searchParams.get('versionA')
  const prefillVersionB = searchParams.get('versionB')
  const anchorSection = searchParams.get('section')

  useEffect(() => {
    if (comparisonId || autoStarted.current) return
    if (!prefillVersionA || !prefillVersionB) return
    autoStarted.current = true
    mutation
      .mutateAsync({
        document_a_version_id: prefillVersionA,
        document_b_version_id: prefillVersionB,
      })
      .then((result) => {
        const suffix = anchorSection
          ? `?section=${encodeURIComponent(anchorSection)}`
          : ''
        navigate(`/app/compare/${result.id}${suffix}`, { replace: true })
      })
      .catch(() => {
        // Pre-fill failed (unauthorized pair etc.) — leave the picker usable
      })
  }, [comparisonId, prefillVersionA, prefillVersionB, anchorSection, mutation, navigate])

  return (
    <div className="comparison-root">
      <div className="page-header" style={{ paddingBottom: 0 }}>
        <div>
          <h1 className="page-title">Document Comparison</h1>
          <p className="page-subtitle">
            Identify what changed between two versions of a document.
          </p>
        </div>
        {comparisonId && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => navigate('/app/compare')}
            id="compare-new-btn"
          >
            + New Comparison
          </button>
        )}
      </div>

      {!comparisonId && !autoStarted.current && (
        <VersionPicker
          onCompare={() => {}}
          prefillDocA={searchParams.get('documentA') ?? ''}
          prefillDocB={searchParams.get('documentB') ?? ''}
        />
      )}

      {comparisonId && <ComparisonDetail comparisonId={comparisonId} />}
    </div>
  )
}
