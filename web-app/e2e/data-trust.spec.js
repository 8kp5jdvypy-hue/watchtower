import { test, expect } from '@playwright/test'
import { mockLoggedIn } from './fixtures.js'

test('backend stale-cache fallback is disclosed beside retained prices', async ({ page }) => {
  await mockLoggedIn(page)
  await page.route('**/quotes*', (route) => route.fulfill({
    status: 200,
    json: {
      quotes: {
        SPY: { symbol: 'SPY', last: 100, bid: 99.9, ask: 100.1, ts: '2026-08-29T14:00:00+00:00' },
        QQQ: { symbol: 'QQQ', last: 200, bid: 199.9, ask: 200.1, ts: '2026-08-29T14:00:00+00:00' },
      },
      freshness: {
        provider_error: true,
        stale_symbols: ['SPY', 'QQQ'],
        missing_symbols: [],
        checked_at_utc: '2026-08-29T14:01:00+00:00',
      },
    },
  }))

  await page.goto('/')
  await page.getByRole('button', { name: 'Watchlist' }).first().click()
  await expect(page.getByText(/Live prices are delayed/)).toBeVisible()
  await expect(page.getByText('$100.00')).toBeVisible()
})

test('failed signal status never paints the watchlist quiet', async ({ page }) => {
  await mockLoggedIn(page)
  await page.route('**/signals/today', (route) => route.fulfill({
    status: 503,
    json: { error: 'temporarily unavailable' },
  }))
  await page.route('**/quotes*', (route) => route.fulfill({
    status: 200,
    json: {
      quotes: {},
      freshness: {
        provider_error: false,
        stale_symbols: [],
        missing_symbols: ['SPY', 'QQQ'],
        checked_at_utc: '2026-08-29T14:01:00+00:00',
      },
    },
  }))

  await page.goto('/')
  await page.getByRole('button', { name: 'Watchlist' }).first().click()
  await expect(page.getByText(/Signal status is unavailable/)).toBeVisible()
  await expect(page.locator('.wl-quiet')).toHaveCount(0)
  await expect(page.locator('.wl-unknown')).toHaveCount(2)
})
