// Phase-3 capture pass: the full screens-level set. Every app tab at
// 1440 + 390 (plus the unlinked-Activity variant, review M11), and the
// landing section-by-section at both widths. Assumes the app dev server
// on :5188 and the landing on :5189.
// Usage: node scripts/capture-phase3.mjs
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const APP = 'http://localhost:5188'
const LANDING = 'http://localhost:5189'
const OUT = '../docs/design-elevation-2026-08/phase3'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const ts = (minAgo) => new Date(now - minAgo * 60000).toISOString()

// Real context_summary shapes (tradebot/api/app.py _HEADLINE_CONTEXT_FIELDS).
const SIGNALS = [
  { id: 'msft-a', symbol: 'MSFT', tier: 'high', trend: 'up', close: 528.4, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { bar_range: 3.12, atr: 0.61 }, headlines: 'MSFT bar range 3.12 is 5.1x ATR(14)=0.61', ts_utc: ts(11), origin: 'watchlist' },
  { id: 'msft-b', symbol: 'MSFT', tier: 'high', trend: 'up', close: 528.9, primary_kind: 'range_expansion', kinds: ['range_expansion', 'rvol_spike'], context_summary: { bar_range: 3.4, atr: 0.61 }, headlines: 'MSFT bar range 3.40 is 5.6x ATR(14)=0.61', ts_utc: ts(10), origin: 'watchlist' },
  { id: 'sig-1', symbol: 'SPY', tier: 'high', trend: 'up', close: 662.4, primary_kind: 'level_break', kinds: ['level_break', 'rvol_spike'], context_summary: { level_name: 'prior_high', level_value: 661.85 }, headlines: 'SPY broke prior_high (661.85) up, 1.42 ATR', ts_utc: ts(24), origin: 'watchlist' },
  { id: 'sig-2', symbol: 'TSLA', tier: 'high', trend: 'down', close: 244.1, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { bar_range: 2.04, atr: 0.33 }, headlines: 'TSLA bar range 2.04 is 6.2x ATR(14)=0.33', ts_utc: ts(37), origin: 'watchlist' },
  { id: 'sig-3', symbol: 'IONQ', tier: 'medium', trend: 'up', close: 14.62, primary_kind: 'rvol_spike', kinds: ['rvol_spike'], context_summary: { cum_volume: 3_100_000, baseline: 1_000_000 }, headlines: 'IONQ cumulative volume 3,100,000 is 3.1x the 12-bar average (1,000,000)', ts_utc: ts(58), origin: 'watchlist' },
  { id: 'sig-4', symbol: 'PLTR', tier: 'medium', trend: 'up', close: 158.9, primary_kind: 'vwap_break', kinds: ['vwap_break'], context_summary: { vwap: 158.61 }, headlines: 'PLTR broke up VWAP (158.61), 0.88 ATR', ts_utc: ts(87), origin: 'screening' },
  { id: 'sig-5', symbol: 'BE', tier: 'medium', trend: 'down', close: 22.31, primary_kind: 'round_number_break', kinds: ['round_number_break'], context_summary: { level: 22.5 }, headlines: 'BE crossed down round number 22.50, 0.61 ATR past it', ts_utc: ts(140), origin: 'watchlist' },
  { id: 'sig-6', symbol: 'QQQ', tier: 'medium', trend: 'up', close: 601.2, primary_kind: 'relative_strength_break', kinds: ['relative_strength_break'], context_summary: { market_proxy: 'SPY', divergence: 0.42, atr: 0.52 }, headlines: 'QQQ outperforming SPY by 0.81 ATR since the open', ts_utc: ts(190), origin: 'watchlist' },
]

const QUOTES = { SPY: { last: 664.51 }, QQQ: { last: 602.77 }, GOOGL: { last: 207.32 }, TSLA: { last: 241.05 }, BE: { last: 22.05 }, IONQ: { last: 14.91 }, PLTR: { last: 160.44 }, MSFT: { last: 531.2 } }

const RESPONSES = {
  '**/me': { id: 'acct-1', email: 'you@example.com', plan: 'beta', founding_member: true, linked_identities: [{ provider: 'telegram', display: '@you' }] },
  '**/watchlist': { symbols: ['SPY', 'QQQ', 'GOOGL', 'TSLA', 'BE', 'IONQ'], is_custom: true },
  '**/signals/today': { session: '2026-08-14', signals: SIGNALS.slice(2, 6) },
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

async function mock(page, overrides = {}) {
  for (const [pattern, body] of Object.entries({ ...RESPONSES, ...overrides })) {
    await page.route(pattern, (route) => route.fulfill({ status: 200, json: body }))
  }
}

async function shoot(page, name, delay = 1000) {
  await page.waitForTimeout(delay)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('captured', name)
}

const browser = await chromium.launch()

// ---------------- App: every tab, both breakpoints ----------------
for (const [label, viewport] of [['1440', { width: 1440, height: 900 }], ['390', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await mock(page)
  await page.goto(APP)
  await shoot(page, `app-${label}-today`)
  const selector = viewport.width < 720 ? '.mobile-nav-button' : '.tab-button'
  for (const tab of ['Watchlist', 'Signals', 'Journal', 'Performance', 'Activity', 'Settings']) {
    await page.locator(selector, { hasText: tab }).first().click()
    await shoot(page, `app-${label}-${tab.toLowerCase()}`)
  }
  await ctx.close()

  // The unlinked-Activity variant (review M11: h1 + subtitle retained).
  const ctx2 = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page2 = await ctx2.newPage()
  await mock(page2, { '**/activity': { trades: [], stats: null } })
  await page2.goto(APP)
  await page2.waitForTimeout(600)
  await page2.locator(selector, { hasText: 'Activity' }).first().click()
  await shoot(page2, `app-${label}-activity-unlinked`)
  await ctx2.close()
}

// ---------------- Landing: section by section ----------------
const SECTIONS = [
  ['hero', null, 0],
  ['field', '#field', 200],
  ['signal-pin-open', '#signal', 0],     // the pin's opening beat -- M10's fixed frame
  ['signal-pin-mid', '#signal', 900],    // mid-pin, context beat
  ['manifesto', '.manifesto', 100],
  ['value', '#value', 100],
  ['interface', '#interface', 100],
  ['coverage', '#coverage', 100],
  ['pricing', '#pricing', 100],
  ['footer', '.site-footer', 0],
]

for (const [label, viewport] of [['1440', { width: 1440, height: 900 }], ['390', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.goto(LANDING)
  await page.waitForTimeout(4500) // boot + hero settle
  for (const [name, sel, extra] of SECTIONS) {
    if (sel) {
      await page.evaluate((s) => document.querySelector(s)?.scrollIntoView(), sel)
      if (extra) await page.evaluate((px) => window.scrollBy(0, px), extra)
    } else if (name === 'hero') {
      await page.evaluate(() => window.scrollTo(0, 0))
    }
    await shoot(page, `landing-${label}-${name}`, 1200)
  }
  await ctx.close()
}

await browser.close()
console.log('done ->', OUT)
