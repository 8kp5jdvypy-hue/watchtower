// Phase-4 capture pass: the reduced-motion sweep (both surfaces) and
// mobile-QA supplements. Reduced-motion shots matter because the
// landing's fallbacks are narrative relayouts, not just killed
// animations -- they have their own layout that has to survive the
// elevation. Assumes app on :5188, landing on :5189.
// Usage: node scripts/capture-phase4.mjs
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const APP = 'http://localhost:5188'
const LANDING = 'http://localhost:5189'
const OUT = '../docs/design-elevation-2026-08/phase4'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const ts = (minAgo) => new Date(now - minAgo * 60000).toISOString()

const SIGNALS = [
  { id: 'sig-1', symbol: 'SPY', tier: 'high', trend: 'up', close: 662.4, primary_kind: 'level_break', kinds: ['level_break', 'rvol_spike'], context_summary: { level_name: 'prior_high', level_value: 661.85 }, headlines: 'SPY broke prior_high (661.85) up, 1.42 ATR', ts_utc: ts(24), origin: 'watchlist' },
  { id: 'sig-2', symbol: 'TSLA', tier: 'high', trend: 'down', close: 244.1, primary_kind: 'range_expansion', kinds: ['range_expansion'], context_summary: { bar_range: 2.04, atr: 0.33 }, headlines: 'TSLA bar range 2.04 is 6.2x ATR(14)=0.33', ts_utc: ts(37), origin: 'watchlist' },
  { id: 'sig-3', symbol: 'IONQ', tier: 'medium', trend: 'up', close: 14.62, primary_kind: 'rvol_spike', kinds: ['rvol_spike'], context_summary: { cum_volume: 3_100_000, baseline: 1_000_000 }, headlines: 'IONQ cumulative volume 3,100,000 is 3.1x the 12-bar average (1,000,000)', ts_utc: ts(58), origin: 'watchlist' },
]

const RESPONSES = {
  '**/me': { id: 'acct-1', email: 'you@example.com', plan: 'beta', founding_member: true, linked_identities: [] },
  '**/watchlist': { symbols: ['SPY', 'QQQ', 'GOOGL', 'TSLA', 'BE', 'IONQ'], is_custom: true },
  '**/signals/today': { session: '2026-08-14', signals: SIGNALS },
  '**/signals/feed*': { signals: SIGNALS },
  '**/quotes*': { quotes: { SPY: { last: 664.51 }, TSLA: { last: 241.05 }, IONQ: { last: 14.91 }, QQQ: { last: 602.77 }, GOOGL: { last: 207.32 }, BE: { last: 22.05 } } },
  '**/signals/sig-2': {
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
  },
  '**/activity': { trades: [], stats: null },
  '**/performance': { by_tier: {}, track_record: null },
  '**/journal/summary': {
    summary: {
      today: { pnl_cents: 18450, trade_count: 3, wins: 2, losses: 1 },
      week: { pnl_cents: 42210, trade_count: 9 },
      month: { pnl_cents: -12800, trade_count: 21 },
      all_time: { pnl_cents: 158900, trade_count: 87 },
    },
    stats: { meaningful: true, total_trades: 87, winning_trades: 48, losing_trades: 39, win_rate: 0.55, avg_win_cents: 6120, avg_loss_cents: -4310 },
  },
  '**/journal/calendar*': { days: {} },
  '**/journal/trades*': { trades: [] },
  '**/journal/linkable-signals*': { delivery_history: false, signals: [] },
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

// ---------- Reduced motion: the landing's narrative fallbacks ----------
for (const [label, viewport] of [['1440', { width: 1440, height: 900 }], ['390', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(LANDING)
  await page.waitForTimeout(2500)
  await shoot(page, `rm-landing-${label}-hero`, 500)
  for (const [name, sel] of [['signal', '#signal'], ['manifesto', '.manifesto'], ['pricing', '#pricing'], ['footer', '.site-footer']]) {
    await page.evaluate((s) => document.querySelector(s)?.scrollIntoView(), sel)
    await shoot(page, `rm-landing-${label}-${name}`, 800)
  }
  await ctx.close()
}

// ---------- Reduced motion: the app ----------
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await mock(page)
  await page.goto(APP)
  await shoot(page, 'rm-app-1440-today')
  await page.locator('.tab-button', { hasText: 'Journal' }).first().click()
  await shoot(page, 'rm-app-1440-journal')
  await page.locator('.tab-button', { hasText: 'Signals' }).first().click()
  await page.locator('.signal-card', { hasText: 'TSLA' }).first().click()
  await shoot(page, 'rm-app-1440-signal-detail')
  await ctx.close()
}

// ---------- Mobile QA supplements ----------
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true })
  const page = await ctx.newPage()
  await page.goto(LANDING)
  await page.waitForTimeout(4500)
  await shoot(page, 'qa-landing-390-hero') // H1 regression check: edge padding, unclipped marks
  await ctx.close()

  const ctx2 = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true })
  const page2 = await ctx2.newPage()
  await mock(page2)
  await page2.goto(APP)
  await page2.waitForTimeout(800)
  await page2.locator('.mobile-nav-button', { hasText: 'Signals' }).first().click()
  await page2.waitForTimeout(400)
  await page2.locator('.signal-card', { hasText: 'TSLA' }).first().click()
  await shoot(page2, 'qa-app-390-signal-detail') // sheet + chrome layering + marks ledger
  await ctx2.close()
}

await browser.close()
console.log('done ->', OUT)
