import test from 'node:test'
import assert from 'node:assert/strict'

import {
  classifyQuoteFreshness,
  normalizeQuoteResponse,
  quoteStatusMessage,
} from '../src/quoteFreshness.js'

const base = {
  requestedCount: 2,
  quoteCount: 2,
  loading: false,
  error: null,
  lastSuccessAt: 1_000,
  freshness: { providerError: false, staleSymbols: [], missingSymbols: [] },
  now: 2_000,
  staleAfterMs: 30_000,
}

test('normalizes valid quotes and infers missing symbols for an older API', () => {
  const result = normalizeQuoteResponse(
    { quotes: { SPY: { last: 100, bid: 99.9, ask: 100.1 } } },
    ['SPY', 'QQQ'],
  )
  assert.equal(result.quotes.SPY.last, 100)
  assert.deepEqual(result.freshness.missingSymbols, ['QQQ'])
})

test('rejects malformed quote payloads before they can crash a card', () => {
  assert.throws(() => normalizeQuoteResponse(null, ['SPY']), /invalid quote response/)
  assert.throws(() => normalizeQuoteResponse({ quotes: { SPY: { last: '100' } } }, ['SPY']), /invalid quote/)
})

test('server-disclosed stale cache and provider failure are never live', () => {
  assert.equal(classifyQuoteFreshness({
    ...base,
    freshness: { providerError: true, staleSymbols: ['SPY'], missingSymbols: [] },
  }), 'delayed')
  assert.equal(classifyQuoteFreshness({
    ...base,
    quoteCount: 0,
    freshness: { providerError: true, staleSymbols: [], missingSymbols: ['SPY', 'QQQ'] },
  }), 'unavailable')
})

test('poll failure preserves prices but says reconnecting, and elapsed success says delayed', () => {
  assert.equal(classifyQuoteFreshness({ ...base, error: new Error('network') }), 'reconnecting')
  assert.equal(classifyQuoteFreshness({ ...base, now: 40_001 }), 'delayed')
  assert.match(quoteStatusMessage('reconnecting'), /may be stale/)
  assert.match(quoteStatusMessage('delayed'), /may be stale/)
})

test('partial and total quote absence are explicit', () => {
  assert.equal(classifyQuoteFreshness({
    ...base,
    quoteCount: 1,
    freshness: { providerError: false, staleSymbols: [], missingSymbols: ['QQQ'] },
  }), 'partial')
  assert.equal(classifyQuoteFreshness({
    ...base,
    quoteCount: 0,
    freshness: { providerError: false, staleSymbols: [], missingSymbols: ['SPY', 'QQQ'] },
  }), 'unavailable')
})
