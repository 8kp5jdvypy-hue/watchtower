// Maps a signal's primary detector kind + trend to a plain-English card
// headline. Every builder also accepts an optional `context` argument --
// the list endpoints (/signals/today and /signals/feed) return it as
// context_summary, one or two fields per kind (see tradebot/api/app.py's
// _HEADLINE_CONTEXT_FIELDS), not the full context_json /signals/<id>
// returns. A row with nothing recorded (pre-migration rows, or a backend
// that predates a field) means `context` comes through as undefined or
// partial -- every builder handles that case, not assumes it's populated.
//
// Design review M5: each headline carries the card's own numbers, so
// twenty range-expansion cards read as twenty different facts instead of
// one templated sentence stamped twenty times. Two honesty rules:
// - Never a guessed number: a missing field degrades to the kind's
//   number-free phrasing where that phrasing still adds reading value
//   (direction words), or to null where it would merely restate the
//   eyebrow (range_expansion, rvol_spike) -- null promotes the raw
//   engine sentence, which has the numbers, to the headline slot.
// - Ratios and divergences keep the engine's own units (ATR multiples,
//   x-normal volume) -- per CLAUDE.md, thresholds speak ATR, never
//   invented percentages.
//
// Only real detector kinds (tradebot/detectors.py) get an entry. An
// unmapped kind returns null so the caller can fall back to the raw
// backend headline sentence -- never a guessed description.

const LEVEL_NAMES = {
  prior_high: "yesterday's high",
  prior_low: "yesterday's low",
  opening_range_high: 'the opening range high',
  opening_range_low: 'the opening range low',
  swing_high: 'a recent swing high',
  swing_low: 'a recent swing low',
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const dollars = (v) => `$${v.toFixed(2)}`
// 1dp ratio, with the pointless ".0" dropped: 6.2x, 3x -- never "3.0x".
const times = (v) => `${(Math.round(v * 10) / 10).toString().replace(/\.0$/, '')}×`

const HEADLINE_BUILDERS = {
  // Requires a real level_name -- no context_summary means either a
  // pre-migration row or a genuinely unrecorded level: returning null
  // here (not a vague "a key level") sends this kind through
  // cardHeadline()'s fallback to the raw engine sentence instead of
  // guessing.
  level_break: (trend, context) => {
    if (!context?.level_name) return null
    const levelName = LEVEL_NAMES[context.level_name] || context.level_name.replace(/_/g, ' ')
    const levelValue = num(context.level_value) != null ? ` (${dollars(context.level_value)})` : ''
    const level = `${levelName}${levelValue}`
    if (trend === 'up') return `Broke above ${level}`
    if (trend === 'down') return `Broke below ${level}`
    return `Broke through ${level}`
  },
  vwap_break: (trend, context) => {
    const vwap = num(context?.vwap)
    const at = vwap != null ? ` (${dollars(vwap)})` : ''
    if (trend === 'up') return `Crossed above VWAP${at}`
    if (trend === 'down') return `Crossed below VWAP${at}`
    return `Crossed VWAP${at}`
  },
  // The two kinds whose number-free phrasing merely restates the
  // eyebrow: without their numbers they return null on purpose, so the
  // raw engine sentence (which has the numbers) takes the headline slot
  // instead of "Trading in an unusually wide range" x19 (review M5).
  range_expansion: (trend, context) => {
    const range = num(context?.bar_range)
    const atr = num(context?.atr)
    if (range == null || atr == null || atr <= 0) return null
    return `Trading in a ${dollars(range)} range — ${times(range / atr)} its typical bar`
  },
  rvol_spike: (trend, context) => {
    const cum = num(context?.cum_volume)
    const baseline = num(context?.baseline)
    if (cum == null || baseline == null || baseline <= 0) return null
    return `Volume ${times(cum / baseline)} normal for this point in the session`
  },
  round_number_break: (trend, context) => {
    const level = num(context?.level)
    const target = level != null ? dollars(level) : 'a round number'
    if (trend === 'up') return `Broke above ${target}`
    if (trend === 'down') return `Broke below ${target}`
    return `Crossed ${target}`
  },
  gap: (trend, context) => {
    const size = num(context?.gap_size)
    const prior = num(context?.prior_close)
    const by = size != null ? ` ${dollars(Math.abs(size))}` : ''
    const from = prior != null ? `yesterday's ${dollars(prior)}` : "yesterday's close"
    if (trend === 'up') return `Gapped up${by} from ${from}`
    if (trend === 'down') return `Gapped down${by} from ${from}`
    return `Gapped${by} from ${from}`
  },
  relative_strength_break: (trend, context) => {
    const proxy = context?.market_proxy || 'the broader market'
    const divergence = num(context?.divergence)
    const atr = num(context?.atr)
    const by = divergence != null && atr != null && atr > 0
      ? ` by ${(Math.abs(divergence) / atr).toFixed(1)} ATR since the open`
      : ''
    if (trend === 'up') return `Outperforming ${proxy}${by}`
    if (trend === 'down') return `Underperforming ${proxy}${by}`
    return `Diverging from ${proxy}${by}`
  },
}

// `kind` should be the signal's primary_kind (with kinds[0] as the
// pre-migration fallback -- see SignalCard.jsx).
export function cardHeadline(kind, trend, context) {
  const builder = kind ? HEADLINE_BUILDERS[kind] : null
  return builder ? builder(trend, context) : null
}
