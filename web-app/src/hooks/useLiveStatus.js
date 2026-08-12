import { useEffect, useState } from 'react'

const TICK_MS = 5_000

const ET_TIME_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})

export function formatEtTime(ms) {
  return ms == null ? null : ET_TIME_FORMAT.format(ms)
}

// Classifies real fetch state (from usePolling) plus real market-session
// state (from useMarketClock) into the five states a trust indicator
// actually needs to distinguish -- see CLAUDE-facing brief: LIVE, DATA
// DELAYED, DATA UNAVAILABLE, RECONNECTING, MARKET CLOSED. A market being
// closed is never "unavailable" -- that's a normal state, not a failure.
// "Delayed" is a safety net for a fetch loop that's silently stopped
// firing (a slept laptop the visibility listener didn't catch) even
// without an explicit error, so stale data is never quietly presented as
// current just because the last request happened to succeed.
//
// The staleness check is evaluated before, not instead of, the literal
// `session === 'closed'` check on purpose: useMarketClock's
// sessionState() only ever returns 'closed' on a weekend -- every
// weekday off-hours minute (including the middle of the night) comes
// back as 'pre' or 'post' instead. A *stalled* poll during any non-open
// session deserves the calm MARKET CLOSED state, not an alarming DATA
// DELAYED, since nothing is actually expected to be updating outside
// the open session either way (LiveStatus's compact tooltip already
// looks up the real session label whenever status is 'closed', so this
// doesn't lose the pre-market/after-hours distinction, just the false
// alarm). A *healthy*, recently-succeeded poll outside 'open' still
// reads as 'live' -- unchanged from before -- since polling can be
// working fine even when nothing is happening market-wise.
export function useLiveStatus({ loading, error, hasData, lastSuccessAt, session, intervalMs }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(id)
  }, [])

  if (loading && !hasData) return 'loading'
  if (error && !hasData) return 'unavailable'
  if (error && hasData) return 'reconnecting'
  if (hasData && lastSuccessAt && now - lastSuccessAt > intervalMs * 3) {
    return session === 'open' ? 'delayed' : 'closed'
  }
  if (session === 'closed') return 'closed'
  return 'live'
}
