import PerchMark from './PerchMark'
import './SignalCard.css'

// Matches the `kind` values detectors.py actually produces -- anything
// not in this map falls back to a de-slugged version of the raw value,
// so a new detector kind never renders as a blank tag.
const KIND_LABELS = {
  level_break: 'Level break',
  rvol_spike: 'Volume spike',
  range_expansion: 'Range expansion',
  vwap_break: 'VWAP break',
  round_number_break: 'Round number',
  gap: 'Gap',
}
function kindLabel(kind) {
  return KIND_LABELS[kind] || kind.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function relativeTime(tsUtc) {
  const diffMs = Date.now() - new Date(tsUtc).getTime()
  const minutes = Math.round(diffMs / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return new Date(tsUtc).toLocaleDateString()
}

// tier ('medium' | 'high') is the only priority signal the API actually
// gives us -- no fabricated "importance score" beyond what's real. High
// tier gets the stronger cyan treatment (the brief's "very important:
// cyan edge/bloom"); medium stays quieter.
// The whole card is the click target (not just the "View signal" label)
// so hover/focus feedback on the card body actually means something --
// a single button-as-card, not a card with a small link buried in the
// footer, per the brief's "card should feel like it's transitioning
// into the intelligence detail view" note. Falls back to a plain,
// non-interactive div when no onView is passed.
export default function SignalCard({ signal, onView }) {
  const isHigh = signal.tier === 'high'
  const Root = onView ? 'button' : 'div'
  return (
    <Root
      type={onView ? 'button' : undefined}
      className={`signal-card${isHigh ? ' is-high' : ''}`}
      onClick={onView ? () => onView(signal.id) : undefined}
    >
      <div className="sc-head">
        <span className="sc-head-id">
          <PerchMark size={14} state={isHigh ? 'alert' : 'confirmed'} accent={false} />
          <span className="sc-eyebrow">PERCH DETECTED</span>
        </span>
        <span className={`sc-tier sc-tier-${signal.tier}`}>{signal.tier}</span>
      </div>

      <div className="sc-body">
        <span className={`sc-symbol ${signal.trend === 'up' ? 'trend-up' : 'trend-down'}`}>
          {signal.symbol}
          <span className="sc-trend-arrow" aria-hidden="true">{signal.trend === 'up' ? '▲' : '▼'}</span>
        </span>
        <p className="sc-headline">{signal.headlines}</p>
        {signal.kinds?.length > 0 && (
          <div className="sc-kinds">
            {signal.kinds.map((k) => <span className="sc-kind-tag" key={k}>{kindLabel(k)}</span>)}
          </div>
        )}
      </div>

      <div className="sc-foot">
        <span className="sc-time">{relativeTime(signal.ts_utc)}</span>
        {onView && (
          <span className="sc-view">
            View signal <span className="sc-view-arrow" aria-hidden="true">→</span>
          </span>
        )}
      </div>
    </Root>
  )
}
