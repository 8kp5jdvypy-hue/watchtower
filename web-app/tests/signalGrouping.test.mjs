// collapseDuplicates (design review M6): presentation-layer merge of
// near-duplicate detections. `npm run test:unit` from web-app/.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { collapseDuplicates, DUPLICATE_WINDOW_MS } from '../src/signalGrouping.js'

const T0 = '2026-08-14T14:30:00Z'
const plus = (ms) => new Date(new Date(T0).getTime() + ms).toISOString()

const sig = (over = {}) => ({
  id: 'a', symbol: 'MSFT', tier: 'high', trend: 'up',
  kinds: ['range_expansion'], ts_utc: T0, headlines: 'raw', ...over,
})

test('same symbol, same call, same window collapses to one card with the kind union', () => {
  const out = collapseDuplicates([
    sig({ id: 'a', kinds: ['range_expansion'] }),
    sig({ id: 'b', kinds: ['range_expansion', 'rvol_spike'], ts_utc: plus(60_000) }),
  ])
  assert.equal(out.length, 1)
  // The richer card (more kinds) survives with its own id/ts/headline.
  assert.equal(out[0].id, 'b')
  assert.deepEqual(out[0].kinds, ['range_expansion', 'rvol_spike'])
})

test('higher tier survives regardless of kind count', () => {
  const out = collapseDuplicates([
    sig({ id: 'medium-rich', tier: 'medium', kinds: ['range_expansion', 'rvol_spike', 'vwap_break'] }),
    sig({ id: 'high-plain', tier: 'high', kinds: ['level_break'], ts_utc: plus(120_000) }),
  ])
  assert.equal(out.length, 1)
  assert.equal(out[0].id, 'high-plain')
  assert.deepEqual(out[0].kinds, ['level_break', 'range_expansion', 'rvol_spike', 'vwap_break'])
})

test('opposite directional calls never merge', () => {
  const out = collapseDuplicates([sig({ id: 'a', trend: 'up' }), sig({ id: 'b', trend: 'down' })])
  assert.equal(out.length, 2)
})

test('different symbols never merge', () => {
  const out = collapseDuplicates([sig({ id: 'a' }), sig({ id: 'b', symbol: 'SPY' })])
  assert.equal(out.length, 2)
})

test('outside the window stays two cards', () => {
  const out = collapseDuplicates([
    sig({ id: 'a' }),
    sig({ id: 'b', ts_utc: plus(DUPLICATE_WINDOW_MS + 60_000) }),
  ])
  assert.equal(out.length, 2)
})

test('tolerates missing kinds and empty input', () => {
  assert.deepEqual(collapseDuplicates([]), [])
  assert.deepEqual(collapseDuplicates(null), [])
  const out = collapseDuplicates([sig({ id: 'a', kinds: undefined }), sig({ id: 'b', ts_utc: plus(1000) })])
  assert.equal(out.length, 1)
  assert.deepEqual(out[0].kinds, ['range_expansion'])
})
