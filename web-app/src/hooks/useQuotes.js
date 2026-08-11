import { useEffect, useState } from 'react'
import { api } from '../api'

// One-shot fetch, not polling -- matches useApiData's own "a dashboard
// refresh is a page refresh" simplicity choice (see that hook's own
// comment). `symbols` is expected to be a fresh array each render (from
// a .map()), so the dependency is the joined string, not the array
// reference, to avoid re-fetching every render. Returns {} while
// loading, on error, or for an empty symbol list -- callers treat a
// missing quote as "no live price yet," never fabricated.
export function useQuotes(symbols) {
  const [quotes, setQuotes] = useState({})
  const key = symbols.join(',')

  useEffect(() => {
    if (!key) return
    let cancelled = false
    api.quotes(key.split(',')).then((body) => {
      if (!cancelled) setQuotes(body.quotes)
    }).catch(() => {
      if (!cancelled) setQuotes({})
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return quotes
}
