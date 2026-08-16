import { formatEtTime } from '../hooks/useLiveStatus'
import { SESSION_LABEL } from '../hooks/useMarketClock'
import './LiveStatus.css'

// Every word here is earned by a real, checkable condition -- see
// useLiveStatus.js for how `status` gets picked. Never say LIVE unless
// the last poll actually succeeded recently; never say MARKET CLOSED and
// mean "something broke."
const COPY = {
  loading: 'CONNECTING',
  live: 'LIVE',
  delayed: 'DATA DELAYED',
  reconnecting: 'RECONNECTING',
  unavailable: 'DATA UNAVAILABLE',
  closed: 'MARKET CLOSED',
}

// compact: the small chrome-level dot (topbar) -- no subline, title
// attribute carries the detail instead of taking up layout space.
// Full (default): the two-line trust block a hero view earns a spot
// for -- status word + a real last-updated timestamp, never a bare
// pulsing dot with no evidence behind it.
export default function LiveStatus({ status, session, time, lastSuccessAt, compact = false }) {
  const label = COPY[status] || COPY.loading
  const updated = formatEtTime(lastSuccessAt)

  if (compact) {
    const title = status === 'closed'
      ? `${SESSION_LABEL[session] || 'Market closed'}`
      : updated
        ? `${label} — data updated ${updated} ET`
        : label
    return <span className={`live-dot live-dot-${status}`} title={title} aria-hidden="true" />
  }

  // When the status word and the session label are the same phrase
  // (MARKET CLOSED + MARKET CLOSED), the session span keeps only the
  // clock -- one fact, said once.
  const sessionLabel = session ? SESSION_LABEL[session] : null
  const sessionText = sessionLabel === label
    ? (time ? `${time} ET` : null)
    : sessionLabel ? `${sessionLabel}${time ? ` — ${time} ET` : ''}` : null

  return (
    <div className={`live-status live-status-${status}`}>
      <span className="live-status-row">
        <span className={`live-dot live-dot-${status}`} aria-hidden="true" />
        <span className="live-status-label">{label}</span>
        {sessionText && <span className="live-status-session">{sessionText}</span>}
      </span>
      {updated && status !== 'closed' && (
        <span className="live-status-sub">Signals updated {updated} ET</span>
      )}
      {status === 'closed' && updated && (
        <span className="live-status-sub">Last checked {updated} ET</span>
      )}
    </div>
  )
}
