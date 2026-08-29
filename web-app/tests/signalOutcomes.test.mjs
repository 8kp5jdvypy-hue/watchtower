import test from 'node:test'
import assert from 'node:assert/strict'

import {
  afterDetectionRows,
  explicitOutcomeRows,
  outcomeResolutionLabel,
} from '../src/signalOutcomes.js'

test('explicit outcomes preserve all checkpoint states and real prices', () => {
  const rows = explicitOutcomeRows({
    outcomes: [
      { offset_min: 15, at_close: false, status: 'AVAILABLE', price: 101.25 },
      { offset_min: 30, at_close: false, status: 'DATA_UNAVAILABLE', price: null },
      { offset_min: null, at_close: true, status: 'NOT_REACHED_BEFORE_CLOSE', price: null },
    ],
  })

  assert.deepEqual(rows, [
    {
      key: 'offset-15', label: '+15 min after detection', offsetMin: 15,
      mark: { price: 101.25 }, status: 'AVAILABLE',
    },
    {
      key: 'offset-30', label: '+30 min after detection', offsetMin: 30,
      mark: null, status: 'DATA_UNAVAILABLE',
    },
    {
      key: 'close', label: 'At session close', offsetMin: null,
      mark: null, status: 'NOT_REACHED_BEFORE_CLOSE',
    },
  ])
})

test('legacy marks remain compatible while an older API is deployed', () => {
  const rows = afterDetectionRows([
    { offset_min: 30, at_close: false, price: 102 },
    { offset_min: null, at_close: true, price: 104 },
  ])
  assert.equal(rows.length, 4)
  assert.equal(rows[0].mark, undefined)
  assert.equal(rows[1].mark.price, 102)
  assert.equal(rows[3].mark.price, 104)
  assert.deepEqual(explicitOutcomeRows({ marks: [] }), afterDetectionRows([]))
})

test('failure and terminal states never render as pending', () => {
  const cases = [
    ['WAITING_FOR_CLOSE_BATCH', 'Processing after session close'],
    ['NOT_REACHED_BEFORE_CLOSE', 'Not reached before session close'],
    ['DATA_UNAVAILABLE', 'Outcome unavailable — data issue'],
    ['DELAYED', 'Outcome delayed — check system status'],
    ['UNKNOWN', 'Outcome status unavailable'],
  ]
  for (const [status, expected] of cases) {
    assert.equal(outcomeResolutionLabel(status, 15, '2026-06-15T13:30:00+00:00').full, expected)
  }
})

test('pending labels use target time only before the target passes', () => {
  const detectedAt = '2026-06-15T13:30:00+00:00'
  const beforeTarget = Date.parse('2026-06-15T13:40:00+00:00')
  const afterTarget = Date.parse('2026-06-15T13:50:00+00:00')
  assert.match(outcomeResolutionLabel('PENDING', 15, detectedAt, beforeTarget).full, /^Resolves ~/)
  assert.equal(
    outcomeResolutionLabel('PENDING', 15, detectedAt, afterTarget).full,
    'Resolves after session close',
  )
})
