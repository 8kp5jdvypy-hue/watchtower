import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'

function pct(value) {
  return value == null ? '—' : `${(value * 100).toFixed(0)}%`
}

export default function Activity() {
  const fetchActivity = useCallback(() => api.activity(), [])
  const { data, error, loading } = useApiData(fetchActivity)

  if (loading) return <div className="view"><p className="empty-state">Loading…</p></div>
  if (error) return <div className="view"><p className="empty-state">Couldn't load your activity.</p></div>

  if (data && data.stats === null) {
    return (
      <div className="view">
        <h1>My Activity</h1>
        <p className="empty-state">
          Link your Telegram account (DM the bot <code>/start</code>, then use the same email here) to see
          your personal trade log and stats.
        </p>
      </div>
    )
  }

  const { stats, trades } = data
  const overall = stats?.overall

  return (
    <div className="view">
      <h1>My Activity</h1>
      <p className="view-subtitle">Your own trade log — logged via /took and /closed in Telegram.</p>

      <div className="stat-grid">
        <div className="stat-tile">
          <div className="stat-tile-label">Total trades logged</div>
          <div className="stat-tile-value">{stats.total_trades}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-tile-label">Win rate</div>
          <div className="stat-tile-value">{overall ? pct(overall.win_rate) : '—'}</div>
          {overall && <div className="headline">n={overall.n}</div>}
        </div>
        <div className="stat-tile">
          <div className="stat-tile-label">Adherence to rules</div>
          <div className="stat-tile-value">{pct(stats.adherence_score)}</div>
        </div>
      </div>

      {trades.length === 0 ? (
        <p className="empty-state">No trades logged yet.</p>
      ) : (
        trades.slice(0, 20).map((trade) => (
          <div className="card" key={trade.id}>
            <div className="card-row">
              <span className="symbol">{trade.symbol}</span>
              <span className={trade.pnl_pct > 0 ? 'trend-up' : trade.pnl_pct < 0 ? 'trend-down' : ''}>
                {trade.pnl_pct != null ? `${trade.pnl_pct.toFixed(1)}%` : trade.status}
              </span>
            </div>
          </div>
        ))
      )}
    </div>
  )
}
