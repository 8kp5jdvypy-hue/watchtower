// Dependency-free tests for signalHistory.js via Node's built-in runner:
//
//   node --test web-app/src
//
// Not wired into pytest (this repo has no JS test framework); exists so
// the H5 verdict logic -- which inverted live verdicts once via a
// double-flip -- has an executable regression check at all. If a JS
// framework ever lands, fold these in there.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { FLAT_BAND_ATR, interpretHistory, isRoughlyFlat } from './signalHistory.js'

const base = { sample_size: 12, offset_min: 30, avg_return_pct: 0.8, avg_return_atr: 0.4 }

test('verdict is keyed off continuation_rate alone', () => {
  assert.match(interpretHistory({ ...base, continuation_rate: 0.3 }), /weak/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.5 }), /mixed/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.7 }), /decent/)
})

test('no double-flip: a continuing down-trend (positive signed avg) reads decent, not weak', () => {
  // Backend sends avg_return_pct/atr ALREADY signed to the detection's
  // trend: a down-trend that continued down arrives positive. The old
  // directionalAvgReturn() re-flip turned exactly this history into
  // "Historically weak follow-through" in production.
  const continuingDownTrend = { ...base, continuation_rate: 0.7, avg_return_pct: 1.2, avg_return_atr: 0.5 }
  assert.match(interpretHistory(continuingDownTrend), /decent/)
  // and the means no longer influence the sentence at all
  const contradictoryMeans = { ...base, continuation_rate: 0.7, avg_return_pct: -1.2, avg_return_atr: -0.5 }
  assert.match(interpretHistory(contradictoryMeans), /decent/)
})

test('flat band is judged in ATR units with pct fallback', () => {
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: FLAT_BAND_ATR / 2 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: -FLAT_BAND_ATR / 2 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: FLAT_BAND_ATR * 2 }), false)
  // pre-atr14 rows: fall back to the pct band
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: null, avg_return_pct: 0.05 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: null, avg_return_pct: 0.5 }), false)
})
