import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import PerchMark from './PerchMark'
import './Views.css'

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
        <span className="view-eyebrow"><span className="dot" /> MY ACTIVITY</span>
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>Not linked yet.</h2>
          <p>DM the bot <code>/start</code> on Telegram, then use the same email here, to see your personal trade log and stats.</p>
        </div>
      </div>
    )
  }

  const { stats, trades } = data
  const overall = stats?.overall

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> MY ACTIVITY</span>
      <h1>Your own trade log.</h1>
      <p className="view-subtitle">Logged via /took and /closed in Telegram.</p>

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
        <div className="quiet-state">
          <PerchMark size={26} state="idle" />
          <h2>No trades logged yet.</h2>
          <p>Log a trade with /took in Telegram after you act on a signal, and it'll show up here.</p>
        </div>
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
