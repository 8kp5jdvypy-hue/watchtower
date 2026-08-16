// Phase-1 screenshot pass for the design-elevation program. Same
// approach as the August design review's capture harness: the real app
// served by vite, every API answered by route interception with
// realistic data shapes (see e2e/fixtures.js for the canonical shapes).
// Usage: node scripts/capture-phase1.mjs [baseURL] [outDir]
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://localhost:5188'
const OUT = process.argv[3] || '../docs/design-elevation-2026-08/phase1'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const ts = (minAgo) => new Date(now - minAgo * 60000).toISOString()

const SIGNALS = [
  { id: 'sig-1', symbol: 'SPY', tier: 'high', trend: 'up', close: 662.4, primary_kind: 'level_break', kinds: ['level_break', 'rvol_spike'], context_summary: { level_name: 'prior_high', level_value: 661.85 }, headlines: 'SPY broke above prior high 661.85 on rvol 2.6', ts_utc: ts(12), origin: 'watchlist' },
  { id: 'sig-2', symbol: 'TSLA', tier: 'high', trend: 'down', close: 244.1, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { range_atr: 2.04, range_ratio: 6.2 }, headlines: 'TSLA range 2.04 ATR, 6.2x its typical bar', ts_utc: ts(34), origin: 'watchlist' },
  { id: 'sig-3', symbol: 'IONQ', tier: 'medium', trend: 'up', close: 14.62, primary_kind: 'rvol_spike', kinds: ['rvol_spike'], context_summary: { rvol: 3.1 }, headlines: 'IONQ volume 3.1x normal for this time of day', ts_utc: ts(58), origin: 'watchlist' },
  { id: 'sig-4', symbol: 'PLTR', tier: 'medium', trend: 'up', close: 158.9, primary_kind: 'vwap_break', kinds: ['vwap_break'], context_summary: {}, headlines: 'PLTR crossed above session VWAP', ts_utc: ts(87), origin: 'screening' },
  { id: 'sig-5', symbol: 'BE', tier: 'medium', trend: 'down', close: 22.31, primary_kind: 'round_number_break', kinds: ['round_number_break'], context_summary: {}, headlines: 'BE broke below 22.50', ts_utc: ts(140), origin: 'watchlist' },
  { id: 'sig-6', symbol: 'QQQ', tier: 'medium', trend: 'up', close: 601.2, primary_kind: 'relative_strength_break', kinds: ['relative_strength_break'], context_summary: {}, headlines: 'QQQ outperforming SPY by 0.8 ATR since open', ts_utc: ts(190), origin: 'watchlist' },
]

const QUOTES = { SPY: { last: 664.51 }, QQQ: { last: 602.77 }, GOOGL: { last: 207.32 }, TSLA: { last: 241.05 }, BE: { last: 22.05 }, IONQ: { last: 14.91 }, PLTR: { last: 160.44 } }

const RESPONSES = {
  '**/me': { id: 'acct-1', email: 'you@example.com', plan: 'beta', founding_member: true, linked_identities: [{ provider: 'telegram', display: '@you' }] },
  '**/watchlist': { symbols: ['SPY', 'QQQ', 'GOOGL', 'TSLA', 'BE', 'IONQ'], is_custom: true },
  '**/signals/today': { session: '2026-08-14', signals: SIGNALS.slice(0, 4) },
  '**/signals/feed*': { signals: SIGNALS },
  '**/quotes*': { quotes: QUOTES },
  '**/performance': {
    by_tier: {
      high: { tier: 'HIGH', continuation_rate: 0.63, sample_size: 41, offset_min: 30 },
      medium: { tier: 'MEDIUM', continuation_rate: 0.54, sample_size: 117, offset_min: 30 },
    },
    by_kind: {
      range_expansion: { kind: 'range_expansion', median_return_pct: 0.21, avg_return_pct: 0.34, continuation_rate: 0.58, sample_size: 64, offset_min: 30, excluded_news_driven: 3 },
      rvol_spike: { kind: 'rvol_spike', median_return_pct: 0.18, avg_return_pct: 0.22, continuation_rate: 0.55, sample_size: 48, offset_min: 30, excluded_news_driven: 1 },
      level_break: { kind: 'level_break', median_return_pct: 0.12, avg_return_pct: -0.05, continuation_rate: 0.52, sample_size: 33, offset_min: 30, excluded_news_driven: 0 },
      vwap_break: { kind: 'vwap_break', median_return_pct: -0.08, avg_return_pct: -0.11, continuation_rate: 0.47, sample_size: 21, offset_min: 30, excluded_news_driven: 0 },
      round_number_break: { kind: 'round_number_break', median_return_pct: 0.05, avg_return_pct: 0.09, continuation_rate: 0.51, sample_size: 12, offset_min: 30, excluded_news_driven: 0 },
    },
    track_record: { hit_rate: 0.63, sample_size: 41, avg_return_pct: 0.29, significance: { is_significant: false, z_score: 1.61 } },
  },
  '**/activity': {
    stats: { total_trades: 14, adherence_score: 0.86, overall: { win_rate: 0.57, n: 14 } },
    trades: [
      { id: 't1', symbol: 'SPY', pnl_pct: 1.2, status: 'closed' },
      { id: 't2', symbol: 'TSLA', pnl_pct: -0.8, status: 'closed' },
      { id: 't3', symbol: 'IONQ', pnl_pct: 4.6, status: 'closed' },
      { id: 't4', symbol: 'QQQ', pnl_pct: 0.3, status: 'closed' },
      { id: 't5', symbol: 'BE', pnl_pct: null, status: 'open' },
      { id: 't6', symbol: 'GOOGL', pnl_pct: -1.4, status: 'closed' },
      { id: 't7', symbol: 'SPY', pnl_pct: 0.9, status: 'closed' },
      { id: 't8', symbol: 'TSLA', pnl_pct: 2.1, status: 'closed' },
    ],
  },
  '**/journal/summary': {
    summary: {
      today: { pnl_cents: 18450, trade_count: 3, wins: 2, losses: 1 },
      week: { pnl_cents: 42210, trade_count: 9 },
      month: { pnl_cents: -12800, trade_count: 21 },
      all_time: { pnl_cents: 158900, trade_count: 87 },
    },
    stats: { meaningful: true, total_trades: 87, winning_trades: 48, losing_trades: 39, win_rate: 0.55, avg_win_cents: 6120, avg_loss_cents: -4310 },
  },
  '**/journal/calendar*': {
    days: (() => {
      const d = {}
      const base = new Date()
      const y = base.getFullYear(); const m = String(base.getMonth() + 1).padStart(2, '0')
      const vals = [[3, 12400, 2], [5, -6200, 1], [6, 4100, 3], [10, 800, 1], [11, -3900, 2], [12, 15300, 4], [13, 2100, 1]]
      for (const [day, pnl, n] of vals) d[`${y}-${m}-${String(day).padStart(2, '0')}`] = { pnl_cents: pnl, trade_count: n }
      return d
    })(),
  },
  '**/journal/trades*': { trades: [] },
  '**/journal/linkable-signals*': { delivery_history: false, signals: [] },
}

async function mock(page) {
  for (const [pattern, body] of Object.entries(RESPONSES)) {
    await page.route(pattern, (route) => route.fulfill({ status: 200, json: body }))
  }
}

async function shoot(page, name) {
  await page.waitForTimeout(1000)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('captured', name)
}

const browser = await chromium.launch()

for (const [label, viewport] of [['1440', { width: 1440, height: 900 }], ['390', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await mock(page)

  // Logged-out login screen first (separate context state not needed --
  // the /me mock above is logged-in, so do login via an override context)
  const loginCtx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const loginPage = await loginCtx.newPage()
  await loginPage.route('**/me', (route) => route.fulfill({ status: 401, json: { error: 'unauthorized' } }))
  await loginPage.goto(BASE)
  await shoot(loginPage, `app-${label}-auth-login`)
  await loginCtx.close()

  await page.goto(BASE)
  await shoot(page, `app-${label}-today`)

  const tabs = ['Signals', 'Watchlist', 'Performance', 'Activity', 'Journal', 'Settings']
  for (const tab of tabs) {
    const selector = viewport.width < 720 ? '.mobile-nav-button' : '.tab-button'
    await page.locator(selector, { hasText: tab }).first().click()
    await shoot(page, `app-${label}-${tab.toLowerCase()}`)
  }
  await ctx.close()
}

await browser.close()
console.log('done ->', OUT)
