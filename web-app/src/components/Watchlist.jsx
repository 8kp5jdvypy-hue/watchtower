import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'

export default function Watchlist() {
  const fetchWatchlist = useCallback(() => api.watchlist(), [])
  const { data, error, loading } = useApiData(fetchWatchlist)

  return (
    <div className="view">
      <h1>Watchlist</h1>
      <p className="view-subtitle">
        {data
          ? data.is_custom
            ? 'Your custom watchlist (set via /watchlist in Telegram).'
            : "Perch's default watchlist — link your Telegram account to customize it."
          : 'What Perch is watching for you.'}
      </p>
      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load your watchlist.</p>}
      {data && data.symbols.map((symbol) => (
        <span className="symbol-pill" key={symbol}>{symbol}</span>
      ))}
    </div>
  )
}
