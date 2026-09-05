/**
 * Integrations placeholder — Phase 16 surface (Phase 15 §6.9).
 */
import { SettingsPlaceholder } from './SettingsPlaceholder'

export function IntegrationsPage() {
  return (
    <SettingsPlaceholder
      title="Integrations"
      description="Third-party connectors arrive with the security hardening phase."
      items={[
        'SSO / SAML identity providers',
        'Webhook destinations',
        'API keys and service accounts',
      ]}
    />
  )
}

export default IntegrationsPage

