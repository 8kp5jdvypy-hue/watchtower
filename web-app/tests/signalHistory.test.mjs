// Unit tests for the history interpretation rules (node --test, no
// framework -- run via `npm run test:unit`). These exist because this
// file has already inverted live subscriber-facing language once: the
// 2026-08 double-flip, where the frontend kept compensating for a
// backend sign convention journal.py had already fixed.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  FLAT_BAND_ATR,
  atrFollowThroughLabel,
  interpretHistory,
} from '../src/signalHistory.js'

const WEAK = 'Historically weak follow-through for this setup.'
const MIXED = 'Historically mixed results for this setup.'
const DECENT = 'Historically decent follow-through for this setup.'

function history(continuationRate, avgReturnPct) {
  return { continuation_rate: continuationRate, avg_return_pct: avgReturnPct, sample_size: 14 }
}

test('verdict thresholds key off continuation rate: <0.45 weak, >=0.6 decent, else mixed', () => {
  assert.equal(interpretHistory(history(0.44, 0)), WEAK)
  assert.equal(interpretHistory(history(0.45, 0)), MIXED)
  assert.equal(interpretHistory(history(0.59, 0)), MIXED)
  assert.equal(interpretHistory(history(0.6, 0)), DECENT)
  assert.equal(interpretHistory(history(0.75, 0)), DECENT)
})

test('regression: the live double-flip case -- a down-trend setup that continued reads decent, not weak', () => {
  // Real shape from the Aug 2026 journal (MSFT relative_strength_break
  // down: continuation 0.7, backend-signed avg_return_pct +0.12). The
  // deployed double-flip rendered WEAK for this. The verdict must key
  // off continuation alone, so trend never enters and no sign
  // convention change on avg_return_pct can invert the sentence again.
  assert.equal(interpretHistory(history(0.7, 0.12)), DECENT)
})

test('verdict never consults a signed mean -- sign and magnitude of avg_return_pct are ignored', () => {
  // With small samples both signed means hover near zero where sign is
  // noise; continuation_rate is the robust statistic and the only input.
  assert.equal(interpretHistory(history(0.7, -5)), DECENT)
  assert.equal(interpretHistory(history(0.3, +5)), WEAK)
  assert.equal(interpretHistory(history(0.5, -0.001)), MIXED)
})

test('flat band: below 0.25x ATR the label says roughly flat instead of printing a noise sign', () => {
  assert.equal(atrFollowThroughLabel(0), 'roughly flat on average')
  assert.equal(atrFollowThroughLabel(0.1), 'roughly flat on average')
  assert.equal(atrFollowThroughLabel(-0.15), 'roughly flat on average')
  assert.equal(atrFollowThroughLabel(-0.249), 'roughly flat on average')
})

test('outside the flat band the label is the signed ATR figure, explicit sign both directions', () => {
  assert.equal(atrFollowThroughLabel(0.25), '≈+0.25× ATR')
  assert.equal(atrFollowThroughLabel(0.47), '≈+0.47× ATR')
  assert.equal(atrFollowThroughLabel(-0.25), '≈-0.25× ATR')
  assert.equal(atrFollowThroughLabel(-0.73), '≈-0.73× ATR')
})

test('absent stat renders nothing, not a placeholder', () => {
  assert.equal(atrFollowThroughLabel(null), null)
  assert.equal(atrFollowThroughLabel(undefined), null)
})

test('flat band constant stays a quarter ATR unless re-derived from journal data', () => {
  // 0.25 reproduces the |mean| < 1 SE partition on the Aug 2026 journal
  // samples (per-sample SE of the signed ATR mean: ~0.35-0.83, median
  // ~0.41). If this changes, re-derive against the journal, don't nudge.
  assert.equal(FLAT_BAND_ATR, 0.25)
})
