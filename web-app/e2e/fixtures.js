// Route-interception fixtures for the auth-flow e2e tests. No real
// backend involved -- page.route() answers every API call itself, the
// same response shapes tradebot/api/app.py actually returns (see
// web-app/src/api.js for the real endpoint list), so these tests catch
// a frontend wiring regression without needing Flask, sqlite, or a
// network connection at all.

export const EMPTY_AUTHENTICATED_RESPONSES = {
  '**/watchlist': { symbols: ['SPY', 'QQQ'], is_custom: false },
  '**/signals/today': { session: '2026-01-01', signals: [] },
  '**/signals/feed*': { signals: [] },
  '**/performance': { by_tier: {}, track_record: null },
  '**/activity': { trades: [], stats: null },
}

export async function mockLoggedOut(page) {
  await page.route('**/me', (route) => route.fulfill({ status: 401, json: { error: 'unauthorized' } }))
  await page.route('**/auth/magic-link/request', (route) =>
    route.fulfill({ status: 202, json: { ok: true } })
  )
}

export async function mockLoggedIn(page, account = defaultAccount()) {
  await page.route('**/me', (route) => route.fulfill({ status: 200, json: account }))
  await page.route('**/auth/logout', (route) => route.fulfill({ status: 200, json: { ok: true } }))
  for (const [pattern, body] of Object.entries(EMPTY_AUTHENTICATED_RESPONSES)) {
    await page.route(pattern, (route) => route.fulfill({ status: 200, json: body }))
  }
}

export function defaultAccount() {
  return {
    id: 'acct-e2e-1',
    email: 'e2e@example.com',
    plan: 'beta',
    founding_member: true,
    linked_identities: [],
  }
}
