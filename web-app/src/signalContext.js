// Translates one detector's real context object (see
// tradebot/detectors.py -- every `context={...}` a detector returns)
// into a plain-English sentence plus the raw fields underneath, for the
// signal detail view's "Why Perch flagged this" section. Every value
// used here comes straight from the API response; nothing is invented
// or re-derived from detector thresholds this file doesn't have.

function fmt(n) {
  return typeof n === 'number' ? n.toFixed(2) : String(n ?? '—')
}

const LEVEL_NAMES = {
  prior_high: "yesterday's high",
  prior_low: "yesterday's low",
  opening_range_high: "today's opening-range high",
  opening_range_low: "today's opening-range low",
  swing_high: 'a recent swing high',
  swing_low: 'a recent swing low',
}

function levelName(name) {
  return LEVEL_NAMES[name] || (name ? name.replace(/_/g, ' ') : 'a key level')
}

// One entry per real detector kind (tradebot/detectors.py). Anything not
// listed falls back to a generic explanation below so a new detector
// kind never breaks this view.
const EXPLAINERS = {
  level_break: (ctx) => ({
    plain: `Price broke through ${levelName(ctx.level_name)}${ctx.level != null ? ` ($${fmt(ctx.level)})` : ''}.`,
    technical: [
      ['Level', `${levelName(ctx.level_name)} ($${fmt(ctx.level)})`],
      ['Close', `$${fmt(ctx.close)}`],
      ['ATR(14)', fmt(ctx.atr14)],
      ['Direction', ctx.direction],
    ],
  }),
  rvol_spike: (ctx) => {
    const ratio = ctx.baseline > 0 ? ctx.cum_volume / ctx.baseline : null
    return {
      plain: `Trading volume is running well above its typical pace for this point in the session${ratio ? ` (${ratio.toFixed(1)}× average)` : ''}.`,
      technical: [
        ['Volume so far', Math.round(ctx.cum_volume).toLocaleString()],
        ['Typical by now', Math.round(ctx.baseline).toLocaleString()],
        ['Bar index', ctx.bar_index],
      ],
    }
  },
  range_expansion: (ctx) => ({
    plain: "This bar's price range is unusually wide relative to its typical range.",
    technical: [
      ['Bar range', fmt(ctx.bar_range)],
      ['ATR(14)', fmt(ctx.atr14)],
    ],
  }),
  vwap_break: (ctx) => ({
    plain: 'Price crossed the volume-weighted average price (VWAP) — a level many intraday traders watch closely.',
    technical: [
      ['VWAP', `$${fmt(ctx.vwap)}`],
      ['Close', `$${fmt(ctx.close)}`],
      ['ATR(14)', fmt(ctx.atr14)],
      ['Direction', ctx.direction],
    ],
  }),
  round_number_break: (ctx) => ({
    plain: 'Price broke through a round-number level — a level where orders often cluster.',
    technical: [
      ['Level', `$${fmt(ctx.level)}`],
      ['Close', `$${fmt(ctx.close)}`],
      ['ATR(14)', fmt(ctx.atr14)],
      ['Direction', ctx.direction],
    ],
  }),
  gap: (ctx) => ({
    plain: 'The stock opened well away from its prior close.',
    technical: [
      ['Gap size', `$${fmt(ctx.gap_size)}`],
      ['Prior close', `$${fmt(ctx.prior_close)}`],
      ['Open', `$${fmt(ctx.open)}`],
    ],
  }),
  relative_strength_break: (ctx) => ({
    plain: `Moving independently of the broader market${ctx.market_proxy ? ` — diverging from ${ctx.market_proxy}` : ''}.`,
    technical: [
      ['Compared against', ctx.market_proxy],
      ['Divergence', fmt(ctx.divergence)],
      ['ATR(14)', fmt(ctx.atr14)],
    ],
  }),
}

export function explainContext(kind, context) {
  const explainer = EXPLAINERS[kind]
  if (explainer && context && typeof context === 'object') {
    return explainer(context)
  }
  // Unknown kind, or a context shape from before this view existed --
  // show the label and whatever real fields are there rather than
  // hiding the bullet or guessing at a sentence.
  const label = kind ? kind.replace(/_/g, ' ') : 'signal'
  const technical = context && typeof context === 'object'
    ? Object.entries(context).map(([k, v]) => [k.replace(/_/g, ' '), typeof v === 'number' ? fmt(v) : String(v)])
    : []
  return { plain: `Perch's ${label} detector fired.`, technical }
}
