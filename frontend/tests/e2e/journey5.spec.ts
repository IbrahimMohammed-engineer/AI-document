/**
 * Journey 5 â€” Research Workspace citation sync (Phase 15 Â§9.4).
 *
 * /app/research â†’ ask question â†’ wait for the answer â†’ click a citation
 * badge â†’ the Evidence Panel shows the source snippet WITHOUT navigating
 * away (the URL never changes â€” that is the assertion).
 */

import { expect, test } from '@playwright/test'

const EMAIL = process.env.E2E_EMAIL ?? 'admin@acme.com'
const PASSWORD = process.env.E2E_PASSWORD ?? 'super-secret-1'
const ORG_SLUG = process.env.E2E_ORG_SLUG ?? 'acme'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.getByLabel(/^organization$/i).fill(ORG_SLUG)
  await page.getByLabel(/email/i).fill(EMAIL)
  await page.getByLabel(/password/i).fill(PASSWORD)
  await page.getByRole('button', { name: /sign in/i }).click()
  await expect(page).toHaveURL(/\/app\/dashboard/)
}

test('citation click updates the Evidence Panel in place', async ({ page }) => {
  await login(page)

  await page.goto('/app/research')
  await expect(page).toHaveURL(/\/app\/research/)

  const input = page.getByLabel(/research question/i)
  await input.fill('What is the approval process?')
  await page.getByRole('button', { name: /send/i }).click()

  // Wait for the streamed answer to finish (badges render after done)
  const answer = page.locator('.ask-bubble-assistant').last()
  await expect(answer).toBeVisible({ timeout: 60_000 })
  await expect(answer.locator('.ask-cursor')).toHaveCount(0)

  const badge = answer.locator('.citation-badge').first()
  await expect(badge).toBeVisible()

  // Click the badge â€” the Evidence Panel must update WITHOUT navigation
  const urlBefore = page.url()
  await badge.click()

  const evidence = page.locator('.research-evidence')
  await expect(evidence).toBeVisible()
  await expect(evidence.locator('.research-evidence-text')).toBeVisible({
    timeout: 10_000,
  })

  // THE critical assertion: no URL change (Journey 5 gate)
  expect(page.url()).toBe(urlBefore)
})
