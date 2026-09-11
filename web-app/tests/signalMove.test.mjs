import assert from 'node:assert/strict'
import test from 'node:test'

import { signalMove } from '../src/signalMove.js'

test('live quote movement takes precedence over recorded alert-time movement', () => {
  const move = signalMove(
    {
      close: 100,
      session_move_at_alert_pct: 8,
      session_move_at_alert_status: 'AVAILABLE',
    },
    { last: 103 },
  )

  assert.deepEqual(move, {
    valuePct: 3,
    label: 'since alert',
    source: 'live_quote',
  })
})

test('recorded alert-time movement survives a live quote outage', () => {
  const move = signalMove({
    close: 100,
    session_move_at_alert_pct: -4.25,
    session_move_at_alert_status: 'AVAILABLE',
  })

  assert.deepEqual(move, {
    valuePct: -4.25,
    label: 'at alert',
    source: 'recorded_detection',
  })
})

test('unavailable or legacy movement is never fabricated', () => {
  assert.equal(signalMove({ close: 100 }), null)
  assert.equal(signalMove({
    close: 100,
    session_move_at_alert_pct: null,
    session_move_at_alert_status: 'AVAILABLE',
  }, { last: null }), null)
  assert.equal(signalMove({
    close: 100,
    session_move_at_alert_pct: 9,
    session_move_at_alert_status: 'UNAVAILABLE:invalid_prior_close',
  }), null)
})
