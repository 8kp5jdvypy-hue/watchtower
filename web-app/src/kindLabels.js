// Matches the `kind` values detectors.py actually produces -- anything
// not in this map falls back to a de-slugged version of the raw value,
// so a new detector kind never renders as a blank label.
const KIND_LABELS = {
  level_break: 'Level break',
  rvol_spike: 'Volume spike',
  range_expansion: 'Range expansion',
  vwap_break: 'VWAP break',
  round_number_break: 'Round number',
  gap: 'Gap',
  relative_strength_break: 'Relative strength',
}

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
