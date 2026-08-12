import { useEffect, useState } from 'react'
import { api } from '../api'

// Matches the backend's QUOTE_CACHE_TTL_SECONDS (tradebot/api/app.py) --
// polling faster than that would just re-request the same cached quote.
const QUOTE_POLL_INTERVAL_MS = 10_000

// Polls, not one-shot: a symbol set that stays constant for a whole
// session (the normal case) must not mean "fetched once, displayed
// forever" while the UI's live-status badge keeps claiming LIVE.
// `symbols` is expected to be a fresh array each render (from a
// .map()), so the dependency is the joined string, not the array
// reference, to avoid re-fetching every render. Paused while the tab
// isn't visible, same discipline as usePolling. Returns {} before the
// first successful fetch (or for an empty symbol list) -- callers treat
// a missing quote as "no live price yet," never fabricated -- but a
// later poll failure keeps the last known-good quotes rather than
// blanking them out, since a transient hiccup shouldn't flicker a real
// price to nothing.
export function useQuotes(symbols) {
  const [quotes, setQuotes] = useState({})
  const key = symbols.join(',')

  useEffect(() => {
    if (!key) {
      setQuotes({})
      return
    }
    let cancelled = false
    let hasLoaded = false

    function fetchQuotes() {
      api.quotes(key.split(',')).then((body) => {
        if (cancelled) return
        hasLoaded = true
        setQuotes(body.quotes)
      }).catch(() => {
        if (!cancelled && !hasLoaded) setQuotes({})
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

  return quotes
}
