// Dependency-free tests for signalHistory.js via Node's built-in
// runner: `npm run test:unit` from web-app/.
//
// These lived at src/signalHistory.test.js and were never executed by
// anything -- no test script referenced them, so the file was carrying a
// real regression check that could not fail. Moved here and wired up.
//
// They exist because this module has already inverted live,
// subscriber-facing language once: the 2026-08 double-flip, where the
// frontend kept re-flipping down-trend values for a backend sign
// convention journal.py had already fixed.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { FLAT_BAND_ATR, interpretHistory, isRoughlyFlat } from '../src/signalHistory.js'

const base = { sample_size: 12, offset_min: 30, avg_return_pct: 0.8, avg_return_atr: 0.4 }

test('verdict is keyed off continuation_rate alone', () => {
  assert.match(interpretHistory({ ...base, continuation_rate: 0.3 }), /weak/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.5 }), /mixed/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.7 }), /decent/)
})

test('verdict thresholds hold exactly at their boundaries: <0.45 weak, >=0.6 decent, else mixed', () => {
  // The band edges, not just a value from the middle of each band -- an
  // off-by-one on either comparison changes what a real subscriber reads
  // and would otherwise pass the coarse test above.
  assert.match(interpretHistory({ ...base, continuation_rate: 0.44 }), /weak/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.45 }), /mixed/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.59 }), /mixed/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.6 }), /decent/)
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

test('verdict never consults a signed mean, at any magnitude', () => {
  // With samples capped at 20 rows both signed means hover near zero,
  // where their sign is noise; continuation_rate is the robust statistic
  // and the only input. No sign-convention change on either mean can
  // invert the sentence again.
  assert.match(interpretHistory({ ...base, continuation_rate: 0.7, avg_return_pct: -5 }), /decent/)
  assert.match(interpretHistory({ ...base, continuation_rate: 0.3, avg_return_pct: 5 }), /weak/)
})

test('flat band is judged in ATR units with pct fallback', () => {
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: FLAT_BAND_ATR / 2 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: -FLAT_BAND_ATR / 2 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: FLAT_BAND_ATR * 2 }), false)
  // pre-atr14 rows: fall back to the pct band
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: null, avg_return_pct: 0.05 }), true)
  assert.equal(isRoughlyFlat({ ...base, avg_return_atr: null, avg_return_pct: 0.5 }), false)
})

test('the ATR band is preferred over pct whenever atr14 exists, not merely when pct is absent', () => {
  // A row whose ATR figure is inside the band but whose pct figure is
  // well outside it must read flat -- the pct fallback is for
  // pre-atr14 rows only, never a second opinion.
  assert.equal(
    isRoughlyFlat({ ...base, avg_return_atr: FLAT_BAND_ATR / 2, avg_return_pct: 5 }),
    true,
  )
})
