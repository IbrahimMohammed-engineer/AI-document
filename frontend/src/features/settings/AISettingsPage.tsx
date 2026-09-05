/**
 * AI settings placeholder — Phase 16 surface (Phase 15 §6.9).
 */
import { SettingsPlaceholder } from './SettingsPlaceholder'

export function AISettingsPage() {
  return (
    <SettingsPlaceholder
      title="AI Settings"
      description="Model selection, grounding thresholds and answer-safety configuration arrive with the security hardening phase."
      items={[
        'Default model + provider selection',
        'Groundedness threshold tuning',
        'Answer validation strictness',
      ]}
    />
  )
}

export default AISettingsPage

