/**
 * Playwright configuration (Phase 15 §9.4).
 *
 * E2E journeys run against a SEEDED live environment:
 *   - Frontend dev server: http://localhost:5173
 *   - Backend API:         http://localhost:8000 (VITE_API_BASE_URL)
 *
 * Credentials come from the environment (never committed):
 *   E2E_EMAIL / E2E_PASSWORD / E2E_ORG_SLUG
 */

import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  // Start the dev server automatically unless one is already running
  webServer: process.env.E2E_NO_SERVER
    ? undefined
    : {
        command: 'npm run dev',
        url: 'http://localhost:5173',
        reuseExistingServer: true,
        timeout: 60_000,
      },
})
