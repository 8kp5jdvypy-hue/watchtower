// Maps a signal's primary detector kind (+ trend -- the only two fields
// the list endpoints, /signals/today and /signals/feed, currently return)
// to a plain-English card headline. Every builder also accepts an optional
// `context` argument even though no caller can supply one yet: those list
// endpoints don't return context_json (only /signals/<id> does today), so
// `context` is always undefined for now. Threading the parameter through
// already means wiring in a real level name later (once that field ships)
// is a body-only change inside level_break's builder, not a signature
// change here or at any call site.
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

const HEADLINE_BUILDERS = {
  // Requires a real level_name -- see /signals/<id> and /signals/feed's
  // context_summary (tradebot/api/app.py's _context_summary()). No
  // context_summary means either a pre-migration row or a genuinely
  // unrecorded level: returning null here (not a vague "a key level")
  // sends this kind through cardHeadline()'s fallback to the raw engine
  // sentence instead of guessing.
  level_break: (trend, context) => {
    if (!context?.level_name) return null
    const levelName = LEVEL_NAMES[context.level_name] || context.level_name.replace(/_/g, ' ')
    const levelValue = typeof context.level_value === 'number' ? ` ($${context.level_value.toFixed(2)})` : ''
    const level = `${levelName}${levelValue}`
    if (trend === 'up') return `Broke above ${level}`
    if (trend === 'down') return `Broke below ${level}`
    return `Broke through ${level}`
  },
  vwap_break: (trend) => {
    if (trend === 'up') return 'Crossed above VWAP'
    if (trend === 'down') return 'Crossed below VWAP'
    return 'Crossed VWAP'
  },
  range_expansion: () => 'Trading in an unusually wide range',
  rvol_spike: () => 'Volume running well above normal',
  round_number_break: (trend) => {
    if (trend === 'up') return 'Broke above a round number'
    if (trend === 'down') return 'Broke below a round number'
    return 'Crossed a round number'
  },
  gap: (trend) => {
    if (trend === 'up') return "Gapped up from yesterday's close"
    if (trend === 'down') return "Gapped down from yesterday's close"
    return "Gapped from yesterday's close"
  },
  relative_strength_break: (trend) => {
    if (trend === 'up') return 'Outperforming the broader market'
    if (trend === 'down') return 'Underperforming the broader market'
    return 'Diverging from the broader market'
  },
}

// `kind` should be the signal's first-listed detector kind (kinds[0] at
// call sites) -- list payloads have no primary_kind field, so first-listed
// is the best real proxy available today.
export function cardHeadline(kind, trend, context) {
  const builder = kind ? HEADLINE_BUILDERS[kind] : null
  return builder ? builder(trend, context) : null
}
