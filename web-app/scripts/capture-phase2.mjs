// Phase-2 capture pass: feed de-templating (M5+M6), mobile marks table
// (M8), landing nav bleed-through (H3). Same harness approach as
// capture-phase1.mjs. Assumes the app dev server on :5188 and the
// landing dev server on :5189.
// Usage: node scripts/capture-phase2.mjs
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const APP = 'http://localhost:5188'
const LANDING = 'http://localhost:5189'
const OUT = '../docs/design-elevation-2026-08/phase2'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const ts = (minAgo) => new Date(now - minAgo * 60000).toISOString()

// Real context_summary shapes per tradebot/api/app.py's
// _HEADLINE_CONTEXT_FIELDS after the M5 extension.
const SIGNALS = [
  // The M6 demonstration pair: same symbol, same call, one bar apart,
  // one with an extra kind -- exactly the review's two-MSFT-cards case.
  // Renders as ONE card wearing both tags.
  { id: 'msft-a', symbol: 'MSFT', tier: 'high', trend: 'up', close: 528.4, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { bar_range: 3.12, atr: 0.61 }, headlines: 'MSFT bar range 3.12 is 5.1x ATR(14)=0.61', ts_utc: ts(11), origin: 'watchlist' },
  { id: 'msft-b', symbol: 'MSFT', tier: 'high', trend: 'up', close: 528.9, primary_kind: 'range_expansion', kinds: ['range_expansion', 'rvol_spike'], context_summary: { bar_range: 3.4, atr: 0.61 }, headlines: 'MSFT bar range 3.40 is 5.6x ATR(14)=0.61', ts_utc: ts(10), origin: 'watchlist' },
  { id: 'sig-1', symbol: 'SPY', tier: 'high', trend: 'up', close: 662.4, primary_kind: 'level_break', kinds: ['level_break', 'rvol_spike'], context_summary: { level_name: 'prior_high', level_value: 661.85 }, headlines: 'SPY broke prior_high (661.85) up, 1.42 ATR', ts_utc: ts(24), origin: 'watchlist' },
  { id: 'sig-2', symbol: 'TSLA', tier: 'high', trend: 'down', close: 244.1, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { bar_range: 2.04, atr: 0.33 }, headlines: 'TSLA bar range 2.04 is 6.2x ATR(14)=0.33', ts_utc: ts(37), origin: 'watchlist' },
  { id: 'sig-3', symbol: 'IONQ', tier: 'medium', trend: 'up', close: 14.62, primary_kind: 'rvol_spike', kinds: ['rvol_spike'], context_summary: { cum_volume: 3_100_000, baseline: 1_000_000 }, headlines: 'IONQ cumulative volume 3,100,000 is 3.1x the 12-bar average (1,000,000)', ts_utc: ts(58), origin: 'watchlist' },
  { id: 'sig-4', symbol: 'PLTR', tier: 'medium', trend: 'up', close: 158.9, primary_kind: 'vwap_break', kinds: ['vwap_break'], context_summary: { vwap: 158.61 }, headlines: 'PLTR broke up VWAP (158.61), 0.88 ATR', ts_utc: ts(87), origin: 'screening' },
  { id: 'sig-5', symbol: 'BE', tier: 'medium', trend: 'down', close: 22.31, primary_kind: 'round_number_break', kinds: ['round_number_break'], context_summary: { level: 22.5 }, headlines: 'BE crossed down round number 22.50, 0.61 ATR past it', ts_utc: ts(140), origin: 'watchlist' },
  { id: 'sig-6', symbol: 'QQQ', tier: 'medium', trend: 'up', close: 601.2, primary_kind: 'relative_strength_break', kinds: ['relative_strength_break'], context_summary: { market_proxy: 'SPY', divergence: 0.42, atr: 0.52 }, headlines: 'QQQ outperforming SPY by 0.81 ATR since the open', ts_utc: ts(190), origin: 'watchlist' },
  // A pre-migration row: no context_summary -- range_expansion headline
  // suppressed, raw engine line promoted (the honest-fallback path).
  { id: 'sig-7', symbol: 'GOOGL', tier: 'medium', trend: 'up', close: 206.8, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: null, headlines: 'GOOGL bar range 1.44 is 3.9x ATR(14)=0.37', ts_utc: ts(230), origin: 'watchlist' },
]

// M8: detection from ~40 minutes ago -- +15m backfilled? No: marks only
// land at session close, so a realistic mid-session detail has ALL
// checkpoints pending; but the M8 capture should show both row species,
// so this one is from a prior session with +15m/+30m real and +60m/close
// pending (a late-session detection whose 60m checkpoint never arrived
// before close -- a real shape backfill_marks produces).
const DETAIL = {
  id: 'sig-2', ts_utc: ts(37), session: '2026-08-14', symbol: 'TSLA',
  kinds: ['range_expansion'], contexts: [{ bar_range: 2.04, atr14: 0.33 }],
  headlines: 'TSLA bar range 2.04 is 6.2x ATR(14)=0.33',
  score: 6.2, tier: 'high', trend: 'down', alerted: true, close: 244.1, atr14: 0.33,
  no_trade: false, news_driven: false, event_kind: null, event_severity: null,
  history: { sample_size: 18, continuation_rate: 0.61, avg_return_pct: -0.22, avg_return_atr: -0.4, median_return_pct: -0.18, offset_min: 30 },
  marks: [
    { offset_min: 15, at_close: false, price: 243.61 },
    { offset_min: 30, at_close: false, price: 243.02 },
  ],
  origin: 'watchlist',
}

const QUOTES = { SPY: { last: 664.51 }, QQQ: { last: 602.77 }, GOOGL: { last: 207.32 }, TSLA: { last: 241.05 }, BE: { last: 22.05 }, IONQ: { last: 14.91 }, PLTR: { last: 160.44 }, MSFT: { last: 531.2 } }

const RESPONSES = {
  '**/me': { id: 'acct-1', email: 'you@example.com', plan: 'beta', founding_member: true, linked_identities: [] },
  '**/watchlist': { symbols: ['SPY', 'QQQ', 'GOOGL', 'TSLA', 'BE', 'IONQ'], is_custom: true },
  '**/signals/today': { session: '2026-08-14', signals: SIGNALS.slice(0, 5) },
  '**/signals/feed*': { signals: SIGNALS },
  '**/signals/sig-2': DETAIL,
  '**/quotes*': { quotes: QUOTES },
  '**/activity': { trades: [], stats: null },
  '**/performance': { by_tier: {}, track_record: null },
}

async function mock(page) {
  for (const [pattern, body] of Object.entries(RESPONSES)) {
    await page.route(pattern, (route) => route.fulfill({ status: 200, json: body }))
  }
}

async function shoot(page, name, delay = 1000) {
  await page.waitForTimeout(delay)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('captured', name)
}

const browser = await chromium.launch()

// ---- App: feed (M5+M6) at 1440, signal detail (M8) at 390 + 1440 ----
for (const [label, viewport] of [['1440', { width: 1440, height: 900 }], ['390', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await mock(page)
  await page.goto(APP)
  await page.waitForTimeout(800)
  const selector = viewport.width < 720 ? '.mobile-nav-button' : '.tab-button'
  await page.locator(selector, { hasText: 'Signals' }).first().click()
  await shoot(page, `app-${label}-signals`)
  // Open the TSLA card (sig-2 fixture) for the marks table.
  await page.locator('.signal-card', { hasText: 'TSLA' }).first().click()
  await shoot(page, `app-${label}-signal-detail`)
  await ctx.close()
}

// ---- Landing: H3 scrolled-nav over pinned headlines ----
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.goto(LANDING)
  await page.waitForTimeout(4500) // boot sequence + hero settle
  // Past the section top, so the pinned "beta." headline actually sits
  // under the scrolled nav -- the exact frame the review's H3 cites.
  await page.evaluate(() => document.querySelector('#pricing')?.scrollIntoView())
  await page.evaluate(() => window.scrollBy(0, 520))
  await shoot(page, 'landing-1440-pricing', 1500)
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))
  await shoot(page, 'landing-1440-footer', 1500)
  await ctx.close()
}

await browser.close()
console.log('done ->', OUT)
