/**
 * Settings pages documented as arriving in Phase 16 (§6.9).
 *
 * Each renders the same "documented placeholder" surface: what the section
 * will contain once Phase 16 security/integrations work lands. Kept as four
 * routeable components so deep links resolve.
 */

export function SettingsPlaceholder({
  title,
  description,
  items,
}: {
  title: string
  description: string
  items: string[]
}) {
  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">{title}</h1>
          <p className="page-subtitle">{description}</p>
        </div>
      </div>
      <div className="card empty-state">
        <div className="empty-state-icon" aria-hidden="true">
          🔒
        </div>
        <div className="empty-state-title">Coming in Phase 16</div>
        <p className="empty-state-description">{description}</p>
        <ul className="settings-placeholder-list">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
    </div>
  )
}
