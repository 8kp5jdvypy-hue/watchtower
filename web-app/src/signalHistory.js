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

const NEAR_ZERO_PCT = 0.1

// avg_return_pct from historical_performance() is a RAW signed return, not
// flipped for the signal's own direction (see that function's docstring:
// continuation_rate IS direction-aware, avg_return_pct is not). For a
// "down" trend signal, a good outcome (price kept falling) shows up as a
// NEGATIVE avg_return_pct. This flips it to "average return in the
// signal's own direction" before judging it -- interpreting the existing
// number correctly, not computing a new one.
function directionalAvgReturn(avgReturnPct, trend) {
  return trend === 'down' ? -avgReturnPct : avgReturnPct
}

export function interpretHistory(history, trend) {
  const dirAvg = directionalAvgReturn(history.avg_return_pct, trend)
  const nearZero = Math.abs(dirAvg) < NEAR_ZERO_PCT
  if (history.continuation_rate < 0.45 || (dirAvg < 0 && !nearZero)) {
    return 'Historically weak follow-through for this setup.'
  }
  if (history.continuation_rate >= 0.6 && dirAvg > 0 && !nearZero) {
    return 'Historically decent follow-through for this setup.'
  }
  return 'Historically mixed results for this setup.'
}
