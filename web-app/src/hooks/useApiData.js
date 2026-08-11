import { useEffect, useState } from 'react'

// Every view here is read-only (see the plan's "explicitly not a
// trading terminal" note) — this one hook covers all of them: call the
// API on mount, track loading/error/data, done. No caching, no
// revalidation-on-focus — a dashboard refresh is a page refresh, which
// is the right amount of complexity for a beta with a handful of users.
export function useApiData(fetchFn, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchFn()
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading }
}
