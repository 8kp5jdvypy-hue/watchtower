import { useEffect, useState } from 'react'
import { api } from '../api'
import {
  classifyQuoteFreshness,
  normalizeQuoteResponse,
  QUOTE_POLL_INTERVAL_MS,
} from '../quoteFreshness'

// Matches the backend's QUOTE_CACHE_TTL_SECONDS (tradebot/api/app.py) --
// polling faster than that would just re-request the same cached quote.
const STATUS_TICK_MS = 5_000

// Polls, not one-shot: a symbol set that stays constant for a whole
// session (the normal case) must not mean "fetched once, displayed
// forever" while the UI's live-status badge keeps claiming LIVE.
// `symbols` is expected to be a fresh array each render (from a
// .map()), so the dependency is the joined string, not the array
// reference, to avoid re-fetching every render. Paused while the tab
// isn't visible, same discipline as usePolling. Returns {} before the
// first successful fetch (or for an empty symbol list) -- callers treat
// a missing quote as "no live price yet," never fabricated -- but a
// later poll failure keeps the last known-good quotes rather than blanking
// them out. The returned status makes that degradation explicit, including
// backend-disclosed stale-cache/provider failures hidden inside a 200.
export function useQuotes(symbols) {
  const [quotes, setQuotes] = useState({})
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [lastSuccessAt, setLastSuccessAt] = useState(null)
  const [freshness, setFreshness] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  const key = symbols.join(',')

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), STATUS_TICK_MS)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (!key) {
      setQuotes({})
      setError(null)
      setLoading(false)
      setLastSuccessAt(null)
      setFreshness(null)
      return
    }
    let cancelled = false
    let inFlight = false
    let latestRequest = 0
    setQuotes({})
    setError(null)
    setLoading(true)
    setLastSuccessAt(null)
    setFreshness(null)

    function fetchQuotes() {
      if (inFlight) return
      inFlight = true
      const requestId = ++latestRequest
      api.quotes(key.split(',')).then((body) => {
        if (cancelled || requestId !== latestRequest) return
        const normalized = normalizeQuoteResponse(body, key.split(','))
        setQuotes(normalized.quotes)
        setFreshness(normalized.freshness)
        setError(null)
        setLastSuccessAt(Date.now())
      }).catch((fetchError) => {
        if (cancelled || requestId !== latestRequest) return
        setError(fetchError)
      }).finally(() => {
        inFlight = false
        if (!cancelled && requestId === latestRequest) setLoading(false)
      })
    }

    fetchQuotes()
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') fetchQuotes()
    }, QUOTE_POLL_INTERVAL_MS)

    function onVisibility() {
      if (document.visibilityState === 'visible') fetchQuotes()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      cancelled = true
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const status = classifyQuoteFreshness({
    requestedCount: key ? key.split(',').length : 0,
    quoteCount: Object.keys(quotes).length,
    loading,
    error,
    lastSuccessAt,
    freshness,
    now,
  })
  return { quotes, error, loading, lastSuccessAt, freshness, status }
}
