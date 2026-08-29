import { useCallback, useMemo } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { usePolling } from '../hooks/usePolling'
import { useQuotes } from '../hooks/useQuotes'
import { tierWeight } from '../signalOrder'
import PerchMark from './PerchMark'
import QuoteDataNotice from './QuoteDataNotice'
import './Watchlist.css'
import './Views.css'

// Live price per row, via /quotes -- real last-trade price, nothing
// more. No day % change here: the quote the API returns has bid/ask/
// last only, no day-open or prior-close reference to compute a real
// change against, so showing one would mean inventing a baseline. What
// IS real and worth surfacing beyond price: which watchlist names have
// an active signal today, cross-referenced from the (real) signals
// endpoint.
export default function Watchlist() {
  const fetchWatchlist = useCallback(() => api.watchlist(), [])
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading } = useApiData(fetchWatchlist)
  // Signal state is live market data too. Polling gives a transient failure a
  // recovery path; while it is failing, unsignaled rows remain unavailable
  // rather than being relabeled as calmly quiet from stale/absent data.
  const { data: today, error: todayError } = usePolling(fetchToday)
  const quoteState = useQuotes(data?.symbols ?? [])

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
      {todayError && (
        <p className="data-trust-notice data-trust-notice-unavailable" role="alert">
          Signal status is unavailable. Quiet labels are hidden until it reconnects.
        </p>
      )}
      <QuoteDataNotice status={quoteState.status} lastSuccessAt={quoteState.lastSuccessAt} />

      {data && (
        <div className="data-rows">
          {orderedSymbols.map((symbol) => {
            const sig = signaled.get(symbol)
            const quote = quoteState.quotes[symbol]
            return (
              <div className={`data-row wl-row${sig ? ' has-signal' : ''}`} key={symbol}>
                <span className="data-row-symbol">{symbol}</span>
                {/* Always rendered, even quoteless -- the row is a grid, so a
                    missing middle cell would slide the status slot over. */}
                <span className="data-row-num">{quote != null ? `$${quote.last.toFixed(2)}` : ''}</span>
                <span className="wl-status">
                  {sig ? (
                    <span className={`wl-badge wl-badge-${sig.tier}`}>
                      <PerchMark size={11} state={sig.tier === 'high' ? 'alert' : 'confirmed'} accent={false} />
                      Signal
                    </span>
                  ) : todayError ? (
                    <span className="wl-unknown">unavailable</span>
                  ) : !today ? (
                    <span className="wl-unknown">checking</span>
                  ) : (
                    <span className="wl-quiet">quiet</span>
                  )}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
