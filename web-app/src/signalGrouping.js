// Presentation-layer collapse of near-duplicate detections (design
// review M6): two cards for the same symbol, same directional call,
// minutes apart -- usually one detection with an extra kind tag --
// read to a subscriber as a glitchy double-fire. The journal stays
// untouched; only the feed's rendering merges them.
// .js extension so Node's own ESM loader (the unit tests) resolves it,
// not just Vite.
import { tierWeight } from './signalOrder.js'

// Same window a "same minute, same headline" pair actually spans in the
// journal: detections land on 5-minute bar closes, so anything within
// one bar of an existing card is the same market moment, not news.
export const DUPLICATE_WINDOW_MS = 5 * 60 * 1000

// The card that survives is the more informative one: higher tier
// first, then more detector kinds, then the later timestamp (a later
// cluster re-fire carries the more complete picture). Its id, headline,
// context and ts all stay -- the only merged field is the kind list, so
// the surviving card wears every tag the pair earned between them.
function richer(a, b) {
  if (tierWeight(a.tier) !== tierWeight(b.tier)) return tierWeight(a.tier) < tierWeight(b.tier) ? a : b
  const ak = a.kinds?.length ?? 0
  const bk = b.kinds?.length ?? 0
  if (ak !== bk) return ak > bk ? a : b
  return new Date(a.ts_utc) >= new Date(b.ts_utc) ? a : b
}

function mergedKinds(a, b) {
  const out = [...(a.kinds ?? [])]
  for (const k of b.kinds ?? []) if (!out.includes(k)) out.push(k)
  return out
}

export function collapseDuplicates(signals) {
  const groups = []
  for (const signal of signals ?? []) {
    const ts = new Date(signal.ts_utc).getTime()
    const group = groups.find(
      (g) =>
        g.signal.symbol === signal.symbol &&
        g.signal.trend === signal.trend &&
        Math.abs(g.ts - ts) <= DUPLICATE_WINDOW_MS
    )
    if (!group) {
      groups.push({ signal, ts })
      continue
    }
    const keep = richer(group.signal, signal)
    const drop = keep === group.signal ? signal : group.signal
    group.signal = { ...keep, kinds: mergedKinds(keep, drop) }
    group.ts = new Date(group.signal.ts_utc).getTime()
  }
  return groups.map((g) => g.signal)
}
