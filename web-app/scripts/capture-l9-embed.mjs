// L9 regeneration (see web/src/components/ProductInterface.jsx's
// documented process): the REAL dashboard against the REAL API with
// real journal rows -- no route mocking anywhere. Trimmed throwaway
// journal copy selects the varied window; the account is a throwaway
// demo login whose email gets redacted from the topbar, per process.
// Usage: node scripts/capture-l9-embed.mjs <magic-link-token> <outPng>
import { chromium } from '@playwright/test'

const [token, out] = process.argv.slice(2)
if (!token || !out) throw new Error('usage: capture-l9-embed.mjs <token> <outPng>')

const browser = await chromium.launch()
// Same frame as the outgoing embed: 1280x815 at 2x, downscaled after.
const ctx = await browser.newContext({ viewport: { width: 1280, height: 815 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()

await page.goto(`http://localhost:5188/?token=${encodeURIComponent(token)}`)
await page.getByRole('button', { name: 'Confirm sign-in' }).click()
await page.waitForSelector('.app-shell', { timeout: 10_000 })
await page.locator('.tab-button', { hasText: 'Signals' }).first().click()
await page.waitForSelector('.signal-card', { timeout: 10_000 })
// Redaction, per the documented process: the throwaway account's email
// comes out of the topbar entirely (the old embed did the same).
await page.evaluate(() => {
  const span = document.querySelector('.topbar-account span')
  if (span) span.remove()
})
await page.waitForTimeout(1200) // card stagger + settle
await page.screenshot({ path: out })
await browser.close()
console.log('captured', out)
