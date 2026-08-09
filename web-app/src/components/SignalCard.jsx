function relativeTime(tsUtc) {
  const diffMs = Date.now() - new Date(tsUtc).getTime()
  const minutes = Math.round(diffMs / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return new Date(tsUtc).toLocaleDateString()
}

export default function SignalCard({ signal }) {
  return (
    <div className="card">
      <div className="card-row">
        <div>
          <span className={`symbol ${signal.trend === 'up' ? 'trend-up' : 'trend-down'}`}>
            {signal.symbol} {signal.trend === 'up' ? '▲' : '▼'}
          </span>
          <div className="headline">{signal.headlines}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <span className={`tier-chip ${signal.tier}`}>{signal.tier}</span>
          <div className="headline">{relativeTime(signal.ts_utc)}</div>
        </div>
      </div>
    </div>
  )
}
