import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { kindLabel } from '../kindLabels'
import './Views.css'

function pct(value) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

// kind_performance() has no lookback cap (unlike historical_performance(),
// which is capped at 20 -- see signalHistory.js's SMALL_SAMPLE_THRESHOLD
// and its own comment on why 30 doesn't work there). Sample sizes here can
// genuinely exceed 20, so the brief's original n=30 threshold is real and
// meaningful in this context, unlike the modal's.
const KIND_SMALL_SAMPLE_THRESHOLD = 30

export default function Performance() {
  const fetchPerformance = useCallback(() => api.performance(), [])
  const { data, error, loading } = useApiData(fetchPerformance)

  if (loading) return <div className="view"><p className="empty-state">Loading…</p></div>
  if (error) return <div className="view"><p className="empty-state">Couldn't load performance data.</p></div>

  const tiers = data ? Object.values(data.by_tier) : []
  // Most-observed kind first -- the most statistically grounded entries
  // lead on a page whose whole point is honesty about what's proven.
  const kinds = data ? Object.values(data.by_kind).sort((a, b) => b.sample_size - a.sample_size) : []
  const record = data?.track_record

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> PERFORMANCE</span>
      <h1>Real, backfilled outcomes.</h1>
      <p className="view-subtitle">
        A base rate from the journal, not a prediction. Nothing here is reported until there's enough history behind it.
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

      {kinds.length === 0 && <p className="empty-state">Not enough history yet to report performance by signal type.</p>}
      {kinds.length > 0 && (
        <>
          <h1 style={{ marginTop: '1.75rem', fontSize: '1.1rem' }}>By signal type</h1>
          <div className="stat-grid">
            {kinds.map((k) => (
              <div className="stat-tile" key={k.kind}>
                <div className="stat-tile-label">{kindLabel(k.kind)}</div>
                <div className="stat-tile-value">{k.median_return_pct.toFixed(2)}%</div>
                <div className="headline">median follow-through</div>
                <div className="headline">
                  {pct(k.continuation_rate)} continued · avg {k.avg_return_pct.toFixed(2)}% · n={k.sample_size}, +{k.offset_min}m
                </div>
                {k.sample_size < KIND_SMALL_SAMPLE_THRESHOLD && <span className="stat-tile-tag">Small sample</span>}
                {k.excluded_news_driven > 0 && (
                  <div className="headline">
                    Excludes {k.excluded_news_driven} news-driven signal{k.excluded_news_driven === 1 ? '' : 's'}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {record && (
        <>
          <h1 style={{ marginTop: '1.75rem', fontSize: '1.1rem' }}>HIGH-tier track record</h1>
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
