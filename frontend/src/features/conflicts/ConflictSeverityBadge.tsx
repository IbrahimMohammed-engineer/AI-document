/**
 * ConflictSeverityBadge — MAJOR / MODERATE / MINOR chip (Phase 13).
 *
 * Reuses the exact three-tier visual pattern the Phase 12 change-severity
 * badge establishes (same hue per tier), rather than a second badge style.
 */

const SEVERITY_META: Record<
  string,
  { cls: string; icon: string; label: string }
> = {
  MAJOR: { cls: 'conflict-badge--major', icon: '⬆', label: 'Major' },
  MODERATE: { cls: 'conflict-badge--moderate', icon: '→', label: 'Moderate' },
  MINOR: { cls: 'conflict-badge--minor', icon: '↓', label: 'Minor' },
}

export function ConflictSeverityBadge({ severity }: { severity: string }) {
  const meta = SEVERITY_META[severity] ?? SEVERITY_META.MINOR
  return (
    <span className={`conflict-badge ${meta.cls}`}>
      {meta.icon} {meta.label}
    </span>
  )
}
