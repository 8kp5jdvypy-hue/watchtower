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
export function useLiveStatus({ loading, error, hasData, lastSuccessAt, session, intervalMs }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(id)
  }, [])

  if (loading && !hasData) return 'loading'
  if (error && !hasData) return 'unavailable'
  if (error && hasData) return 'reconnecting'
  if (session === 'closed') return 'closed'
  if (hasData && lastSuccessAt && now - lastSuccessAt > intervalMs * 3) return 'delayed'
  return 'live'
}
