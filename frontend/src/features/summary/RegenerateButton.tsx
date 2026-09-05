/**
 * RegenerateButton (FE §6.12) — triggers useRegenerateSummary().
 *
 * Permission-gated (mirrors ConflictResolutionMenu's pattern — hidden for
 * users without `summary:regenerate`).  While the polled summary's status is
 * PENDING/PROCESSING and a prior summary payload is still present, the
 * existing content stays visible DIMMED with a "Regenerating…" banner —
 * never blanked (plan §6.8).
 */

import { useRegenerateSummary } from '@/hooks/queries/useSummary'
import { useAuthStore } from '@/store/authStore'

export function RegenerateButton({
  documentId,
  version,
  regenerating,
}: {
  documentId: string
  version?: number
  regenerating: boolean
}) {
  const permissions = useAuthStore((s) => s.currentUser?.permissions)
  const canRegenerate = Boolean(permissions?.includes('summary:regenerate'))
  const { mutate, isPending } = useRegenerateSummary()

  if (!canRegenerate) return null

  return (
    <button
      type="button"
      className="btn btn-secondary btn-sm"
      disabled={regenerating || isPending}
      onClick={() => mutate({ documentId, version })}
    >
      {regenerating || isPending ? 'Regenerating…' : 'Regenerate'}
    </button>
  )
}
