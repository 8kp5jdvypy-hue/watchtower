import PerchMark from './PerchMark'
import { cardHeadline } from '../signalHeadlines'
import { kindLabel } from '../kindLabels'
import './SignalCard.css'
import './SignalArrival.css'

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
//
// `arrived` toggles the signal-arrival glow directly on this element's
// own className, rather than a caller wrapping this component in an
// extra div. Same key, same component, every render -- so a card
// settling out of its arrival glow is a className change, not a
// remount that would cut the CSS animation off mid-fade (see Today.jsx).
export default function SignalCard({ signal, quote, onView, arrived = false }) {
  const isHigh = signal.tier === 'high'
  const Root = onView ? 'button' : 'div'
  // Real live quote (SIP, via /quotes) against price at detection --
  // both real fields, never estimated. Absent whenever either side is
  // missing: no quote fetched yet, the vendor had none for this symbol,
  // or (rare, legacy rows) close was never recorded.
  const changePct = quote && signal.close != null ? ((quote.last - signal.close) / signal.close) * 100 : null
  // signal.primary_kind is the real column (frozen at write time in
  // runner.py -- not always kinds[0]). Falls back to kinds[0] only for
  // pre-migration rows where primary_kind is null.
  const primaryKind = signal.primary_kind || signal.kinds?.[0]
  const plainHeadline = cardHeadline(primaryKind, signal.trend, signal.context_summary)
  // The eyebrow already names the primary kind below -- listing it again
  // as its own tag would just repeat the same word twice in one card.
  // Secondary kinds (a cluster with more than one detector firing) still
  // get their tag. Filtered by value, not by slicing off index 0 -- the
  // primary kind isn't guaranteed to be first in the kinds list.
  const secondaryKinds = signal.kinds?.filter((k) => k !== primaryKind) ?? []
  return (
    <Root
      type={onView ? 'button' : undefined}
      className={`signal-card${isHigh ? ' is-high' : ''}${arrived ? ' signal-arrival' : ''}`}
      onClick={onView ? () => onView(signal.id) : undefined}
    >
      <div className="sc-head">
        <span className="sc-head-id">
          <PerchMark size={14} state={isHigh ? 'alert' : 'confirmed'} accent={false} />
          <span className="sc-eyebrow">{primaryKind ? kindLabel(primaryKind) : 'PERCH DETECTED'}</span>
        </span>
        <span className={`sc-tier sc-tier-${signal.tier}`}>{signal.tier}</span>
      </div>

      <div className="sc-body">
        <div className="sc-symbol-row">
          <span className={`sc-symbol ${signal.trend === 'up' ? 'trend-up' : 'trend-down'}`}>
            {signal.symbol}
            <span className="sc-trend-arrow" aria-hidden="true">{signal.trend === 'up' ? '▲' : '▼'}</span>
          </span>
          {/* Colored by raw price direction since detection, same
              convention as the modal's "After detection" marks -- not
              flipped for the signal's own directional call. */}
          {changePct != null && (
            <span className={`sc-change ${changePct >= 0 ? 'trend-up' : 'trend-down'}`}>
              {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
            </span>
          )}
        </div>
        <p className="sc-headline">{plainHeadline || signal.headlines}</p>
        {plainHeadline && <p className="sc-headline-raw">{signal.headlines}</p>}
        {secondaryKinds.length > 0 && (
          <div className="sc-kinds">
            {secondaryKinds.map((k) => <span className="sc-kind-tag" key={k}>{kindLabel(k)}</span>)}
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
