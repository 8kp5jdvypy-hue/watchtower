// cardHeadline (design review M5): headlines carry the card's own
// numbers, degrade honestly when context fields are missing.
// `npm run test:unit` from web-app/.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { cardHeadline } from '../src/signalHeadlines.js'

test('range expansion states the range and the ATR multiple', () => {
  assert.equal(
    cardHeadline('range_expansion', 'down', { bar_range: 2.04, atr: 0.33 }),
    'Trading in a $2.04 range — 6.2× its typical bar'
  )
})

test('a whole-number ratio drops the pointless .0', () => {
  assert.equal(
    cardHeadline('rvol_spike', 'up', { cum_volume: 3_000_000, baseline: 1_000_000 }),
    'Volume 3× normal for this point in the session'
  )
})

test('restating kinds return null without their numbers (raw line takes the slot)', () => {
  assert.equal(cardHeadline('range_expansion', 'down', undefined), null)
  assert.equal(cardHeadline('rvol_spike', 'up', { cum_volume: 5 }), null)
  assert.equal(cardHeadline('range_expansion', 'down', { bar_range: 1, atr: 0 }), null)
})

test('direction-carrying kinds keep number-free phrasing as the fallback', () => {
  assert.equal(cardHeadline('vwap_break', 'up', undefined), 'Crossed above VWAP')
  assert.equal(cardHeadline('vwap_break', 'up', { vwap: 451.2 }), 'Crossed above VWAP ($451.20)')
  assert.equal(cardHeadline('round_number_break', 'down', {}), 'Broke below a round number')
  assert.equal(cardHeadline('round_number_break', 'down', { level: 450 }), 'Broke below $450.00')
})

test('relative strength speaks ATR units from divergence dollars', () => {
  assert.equal(
    cardHeadline('relative_strength_break', 'up', { market_proxy: 'SPY', divergence: 0.66, atr: 0.5 }),
    'Outperforming SPY by 1.3 ATR since the open'
  )
  assert.equal(cardHeadline('relative_strength_break', 'down', {}), 'Underperforming the broader market')
})

test('gap names the size and the anchor', () => {
  assert.equal(
    cardHeadline('gap', 'down', { gap_size: -2.1, prior_close: 448.32 }),
    "Gapped down $2.10 from yesterday's $448.32"
  )
})

test('unmapped kind falls through to the raw engine sentence', () => {
  assert.equal(cardHeadline('mystery_kind', 'up', {}), null)
})
