import { useCallback, useEffect, useRef, useState } from 'react'

export const DEFAULT_POLL_INTERVAL_MS = 45_000

// Same fetch-on-mount contract as useApiData, plus a light re-fetch loop
// while the tab is actually visible -- this is what lets "Perch is
// watching" mean something (a new signal can appear without a page
// refresh) without hammering the API from backgrounded tabs. Paused via
// the Page Visibility API rather than a naive setInterval: a laptop lid
// closed on the Signals tab shouldn't keep polling, and coming back
// after minutes away should refresh immediately rather than waiting out
// whatever's left of the last interval.
export function usePolling(fetchFn, { intervalMs = DEFAULT_POLL_INTERVAL_MS, deps = [] } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastSuccessAt, setLastSuccessAt] = useState(null)
  const fetchFnRef = useRef(fetchFn)
  fetchFnRef.current = fetchFn
  const latestTokenRef = useRef(null)

  const runFetch = useCallback(() => {
    const token = {}
    latestTokenRef.current = token
    fetchFnRef.current()
      .then((result) => {
        if (latestTokenRef.current !== token) return
        setData(result)
        setError(null)
        setLastSuccessAt(Date.now())
      })
      .catch((err) => {
        if (latestTokenRef.current !== token) return
        setError(err)
      })
      .finally(() => {
        if (latestTokenRef.current === token) setLoading(false)
      })
  }, [])

  useEffect(() => {
    setLoading(true)
    setData(null)
    setError(null)
    setLastSuccessAt(null)
    runFetch()

    const id = setInterval(() => {
      if (document.visibilityState === 'visible') runFetch()
    }, intervalMs)

    function onVisibility() {
      if (document.visibilityState === 'visible') runFetch()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading, lastSuccessAt }
}
