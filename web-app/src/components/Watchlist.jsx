import { useCallback, useMemo } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { tierWeight } from '../signalOrder'
import PerchMark from './PerchMark'
import './Watchlist.css'
import './Views.css'

// Deliberately no price or % change per row -- the API has no live
// quote data to back that with (only a symbol list), and the brief and
// this whole product are explicit that a real number beats a
// convincing-looking fake one. What IS real and worth surfacing: which
// watchlist names have an active signal today, cross-referenced from
// the (real) signals endpoint.
export default function Watchlist() {
  const fetchWatchlist = useCallback(() => api.watchlist(), [])
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading } = useApiData(fetchWatchlist)
  const { data: today } = useApiData(fetchToday)

  const signaled = useMemo(() => {
    const map = new Map()
    for (const s of today?.signals || []) {
      if (!map.has(s.symbol) || s.tier === 'high') map.set(s.symbol, s)
    }
    return map
  }, [today])

  // Symbols with an active signal float to the top (HIGH before MEDIUM),
  // quiet symbols stay in the watchlist's own order below them -- a stable
  // sort, so "quiet" doesn't also mean "reshuffled every render."
  const orderedSymbols = useMemo(() => {
    if (!data) return []
    return [...data.symbols].sort((a, b) => {
      const wa = signaled.has(a) ? tierWeight(signaled.get(a).tier) : 2
      const wb = signaled.has(b) ? tierWeight(signaled.get(b).tier) : 2
      return wa - wb
    })
  }, [data, signaled])

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> WATCHLIST</span>
      <h1>What Perch is watching for you.</h1>
      <p className="view-subtitle">
        {data
          ? data.is_custom
            ? 'Your custom watchlist (set via /watchlist in Telegram).'
            : "Perch's default watchlist — link your Telegram account to customize it."
          : 'Loading…'}
      </p>

      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load your watchlist.</p>}

      {data && (
        <div className="wl-rows">
          {orderedSymbols.map((symbol) => {
            const sig = signaled.get(symbol)
            return (
              <div className={`wl-row${sig ? ' has-signal' : ''}`} key={symbol}>
                <span className="wl-symbol">{symbol}</span>
                {sig ? (
                  <span className={`wl-badge wl-badge-${sig.tier}`}>
                    <PerchMark size={11} state={sig.tier === 'high' ? 'alert' : 'confirmed'} accent={false} />
                    Signal
                  </span>
                ) : (
                  <span className="wl-quiet">quiet</span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
