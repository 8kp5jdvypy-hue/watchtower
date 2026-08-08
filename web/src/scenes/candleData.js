// Deterministic demo candle set — quiet range, then a clear breakout.
export function buildCandles(seed = 11, n = 30, trigFrac = 0.62) {
  let s = seed
  const rnd = () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff
    return s / 0x7fffffff
  }
  const trig = Math.round(trigFrac * (n - 1))
  const closes = []
  let lvl = 0.5
  for (let i = 0; i < n; i++) {
    if (i <= trig) {
      lvl = Math.max(0.42, Math.min(0.58, lvl + (rnd() - 0.5) * 0.06))
    } else {
      const prog = (i - trig) / (n - 1 - trig)
      lvl = 0.5 + 0.42 * prog + (rnd() - 0.5) * 0.03
    }
    closes.push(Math.max(0.04, Math.min(0.96, lvl)))
  }
  const ohlc = closes.map((c, i) => {
    const o = i > 0 ? closes[i - 1] : c - (rnd() - 0.5) * 0.03
    const hi = Math.max(o, c) + rnd() * 0.03
    const lo = Math.min(o, c) - rnd() * 0.03
    return { o, c, hi, lo, i }
  })
  return { ohlc, trig, n }
}
