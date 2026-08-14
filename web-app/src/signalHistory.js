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

// The verdict sentence keys off continuation_rate ONLY -- the robust
// statistic. It deliberately never consults a signed mean: with samples
// this small, both avg_return_pct and avg_return_atr hover near zero
// where their sign is noise, and an earlier version of this file that
// judged on avg_return_pct also compensated for a backend sign
// convention that journal.py had since fixed, double-flipping down-trend
// setups into inverted verdicts ("weak" for setups that historically
// continued). Continuation rate has no sign to flip and no near-zero
// cliff, so neither failure mode can recur here.
export function interpretHistory(history) {
  if (history.continuation_rate < 0.45) {
    return 'Historically weak follow-through for this setup.'
  }
  if (history.continuation_rate >= 0.6) {
    return 'Historically decent follow-through for this setup.'
  }
  return 'Historically mixed results for this setup.'
}

// Flat band for the ATR parenthetical, in ATR units. Derived from the
// journal's own samples (Aug 2026, offset 30m): the per-sample standard
// error of the signed ATR mean runs ~0.35-0.83 (median ~0.41), and 0.25
// exactly reproduces the |mean| < 1 SE partition on live data -- every
// sample under it is statistically indistinguishable from zero, every
// sample over it is not. Below this, printing a signed number would be
// printing noise, so the label says "roughly flat" instead; sign noise
// structurally cannot flip the language again. It's also a legible unit:
// a quarter of a typical bar's range.
export const FLAT_BAND_ATR = 0.25

// The display label for avg_return_atr (trend-signed by the backend:
// positive = continued in the called direction, per CLAUDE.md's
// ATR-units convention). Returns null when the stat is absent so the
// caller renders nothing rather than a placeholder.
export function atrFollowThroughLabel(avgReturnAtr) {
  if (avgReturnAtr == null) return null
  if (Math.abs(avgReturnAtr) < FLAT_BAND_ATR) return 'roughly flat on average'
  const signed = `${avgReturnAtr > 0 ? '+' : ''}${avgReturnAtr.toFixed(2)}`
  return `≈${signed}× ATR`
}
