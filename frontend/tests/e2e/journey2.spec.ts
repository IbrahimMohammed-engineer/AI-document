/**
 * Journey 2 â€” RELEASE GATE (Phase 15 Â§9.4).
 *
 * Login â†’ Ask "What is the approval process?" â†’ wait for SSE streaming to
 * complete â†’ â‰¥1 citation badge renders â†’ click badge â†’ Document Workspace
 * opens at the cited page with the highlight. Must complete in â‰¤2 clicks
 * from the citation badge to the exact source page.
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

test('citation badge navigates to the source page in â‰¤2 clicks', async ({ page }) => {
  await login(page)

  // Ask AI
  await page.goto('/app/ask')
  const question = 'What is the approval process?'
  const input = page.getByLabel(/ask a question/i)
  await input.fill(question)
  await page.getByRole('button', { name: /send/i }).click()

  // Wait for the streamed answer to complete (citations render after done)
  const answer = page.locator('.ask-bubble-assistant').last()
  await expect(answer).toBeVisible({ timeout: 60_000 })
  await expect(answer.locator('.ask-cursor')).toHaveCount(0)
  await expect(answer.locator('.ask-answer').or(answer.locator('.ask-note'))).toBeVisible()

  // â‰¥1 citation badge renders
  const badge = answer.locator('.citation-badge').first()
  await expect(badge).toBeVisible()

  // Click 1: badge â†’ Document Workspace at ?page=N&q=â€¦
  await badge.click()
  await expect(page).toHaveURL(/\/app\/documents\//)

  // The workspace deep-linked state must include the cited page + query
  const url = new URL(page.url())
  expect(url.searchParams.get('page')).not.toBeNull()
  expect(url.searchParams.get('q')).not.toBeNull()

  // Click 2 (source highlight already on the page): the extracted page is
  // focused with the persistent highlight overlay â€” one explicit assertion
  // that the exact source span is rendered.
  const highlight = page.locator('mark.source-highlight').first()
  await expect(highlight).toBeVisible()
})
