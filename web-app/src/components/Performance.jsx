import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'

function pct(value) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

export default function Performance() {
  const fetchPerformance = useCallback(() => api.performance(), [])
  const { data, error, loading } = useApiData(fetchPerformance)

  if (loading) return <div className="view"><p className="empty-state">Loading…</p></div>
  if (error) return <div className="view"><p className="empty-state">Couldn't load performance data.</p></div>

  const tiers = data ? Object.values(data.by_tier) : []
  const record = data?.track_record

  return (
    <div className="view">
      <h1>Performance</h1>
      <p className="view-subtitle">
        Real, backfilled outcomes from the journal — not a prediction, a base rate. Nothing below is
        reported until there's enough history behind it.
      </p>

      {tiers.length === 0 && <p className="empty-state">Not enough history yet to report tier performance.</p>}
      {tiers.length > 0 && (
        <div className="stat-grid">
          {tiers.map((tier) => (
            <div className="stat-tile" key={tier.tier}>
              <div className="stat-tile-label">{tier.tier} · continuation rate</div>
              <div className="stat-tile-value">{pct(tier.continuation_rate)}</div>
              <div className="headline">n={tier.sample_size}, +{tier.offset_min}m</div>
            </div>
          ))}
        </div>
      )}

      {record && (
        <>
          <h1 style={{ marginTop: '1.5rem' }}>HIGH-tier track record</h1>
          <div className="stat-grid">
            <div className="stat-tile">
              <div className="stat-tile-label">Hit rate</div>
              <div className="stat-tile-value">{pct(record.hit_rate)}</div>
              <div className="headline">n={record.sample_size}</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Avg return</div>
              <div className="stat-tile-value">{record.avg_return_pct.toFixed(2)}%</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Statistically significant?</div>
              <div className="stat-tile-value">{record.significance.is_significant ? 'Yes' : 'Not yet'}</div>
              <div className="headline">z={record.significance.z_score.toFixed(2)}</div>
            </div>
          </div>
        </>
      )}
      {!record && tiers.length > 0 && (
        <p className="empty-state">Not enough HIGH-tier history yet for a full track record.</p>
      )}
    </div>
  )
}
