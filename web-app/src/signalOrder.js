// Real signals only ever carry tier 'high' or 'medium' here (the API's
// list/today endpoints already filter out 'log'/sub-threshold rows) --
// this just orders what's already fetched, it doesn't invent a priority
// score.
const TIER_WEIGHT = { high: 0, medium: 1 }

export function tierWeight(tier) {
  return TIER_WEIGHT[tier] ?? 1
}

// Stable sort (guaranteed by spec since ES2019): signals keep their
// original relative order -- most recent first, per the API's own
// ORDER BY ts_utc DESC -- within each tier.
export function bySeverity(signals) {
  return [...signals].sort((a, b) => tierWeight(a.tier) - tierWeight(b.tier))
}
