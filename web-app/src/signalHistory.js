// Turns the real numbers already in a signal's `history` (see
// /signals/<id> -> historical_performance() in tradebot/journal.py) into
// a small amount of interpretive framing -- a deterministic mapping of
// existing fields, never a new metric.

// historical_performance() caps its lookback at 20 rows, so sample_size is
// always in [MIN_HISTORY_SAMPLE, 20] -- it can never reach a textbook
// "30+" sample. A flag drawn at 30 would fire on every row this view ever
// shows, which defeats the point of a flag. 10 splits the achievable range
// in half: below it, treat the stat as thin evidence.
export const SMALL_SAMPLE_THRESHOLD = 10

// "Roughly flat" band for the average follow-through, in ATR units --
// the project's own unit convention (CLAUDE.md: thresholds in ATR, never
// percentages). Below this magnitude the average is presented as flat
// rather than printed with a sign, because a signed figure that small
// overstates what the data says. Percent fallback only for rows written
// before atr14 existed.
export const FLAT_BAND_ATR = 0.05
const FLAT_BAND_PCT_FALLBACK = 0.1

// avg_return_pct and avg_return_atr both arrive ALREADY signed to the
// detection's own trend (see historical_performance(): a down-trend
// detection that continued down reports positive). This module used to
// re-flip down-trend values itself, from before the backend was fixed --
// with the flipped backend that DOUBLE-flipped them and inverted the
// verdict sentence on every down-trend signal. No direction math happens
// here anymore, on purpose.

export function isRoughlyFlat(history) {
  if (history.avg_return_atr != null) return Math.abs(history.avg_return_atr) < FLAT_BAND_ATR
  return Math.abs(history.avg_return_pct) < FLAT_BAND_PCT_FALLBACK
}

// The verdict sentence is keyed off continuation_rate ALONE: it is the
// one direction-aware, per-row-derived rate in the payload, and a rate
// can't contradict its own sign the way two differently-weighted means
// can (design review H5). The means stay display-only.
export function interpretHistory(history) {
  if (history.continuation_rate < 0.45) {
    return 'Historically weak follow-through for this setup.'
  }
  if (history.continuation_rate >= 0.6) {
    return 'Historically decent follow-through for this setup.'
  }
  return 'Historically mixed results for this setup.'
}
