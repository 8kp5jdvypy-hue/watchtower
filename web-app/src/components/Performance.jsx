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
  // `data.by_kind || {}`: guards a backend that hasn't deployed this
  // field yet (frontend/backend deploys aren't atomic -- see the API's
  // own /performance route), not just a defensive habit.
  const kinds = data ? Object.values(data.by_kind || {}).sort((a, b) => b.sample_size - a.sample_size) : []
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
              {tier.excluded_news_driven > 0 && (
                <div className="headline">
                  Excludes {tier.excluded_news_driven} news-driven signal{tier.excluded_news_driven === 1 ? '' : 's'}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* The only place in the app a user ever meets the word LOG: the
          list endpoints filter sub-threshold rows out entirely (see the
          API's tier IN ('high','medium') and signalOrder.js), so LOG has
          no card of its own to explain itself and would otherwise read
          as a third kind of alert sitting next to MEDIUM.
          Every clause here is deliberately hedged, because the SUBSCRIBER
          delivery path is not the same as the tier decision:
          - HIGH clears the tier bar but is only ELIGIBLE to be sent --
            telegram_bot/delivery.py's make_subscriber_hook filters by
            db.list_subscribers_for_symbol (onboarded, risk-acked, not
            paused/locked/session-halted, symbol on their watchlist,
            reachable) and a 'quiet' subscriber's personal floor sits
            above the global one; alerts.py can still suppress on cap or
            cooldown, and the outbox delivers asynchronously.
          - MEDIUM's hourly digest reaches subscribers only through
            make_medium_fanout_fn, which is 'aggressive' sensitivity
            only -- most users never receive it.
          - LOG has no fan-out function at all. send_log_summary() writes
            to `alerter`, which is the ops channel/console
            (TelegramAlerter/ConsoleAlerter), never a subscriber DM -- so
            this must not imply users get an end-of-day recap.
          - tier_performance() JOINs marks and applies
            CURRENT_FEED_FILTER_SQL, so the rates are neither every
            detection nor only delivered ones: they are the qualifying
            journaled ones, and nothing filters on `alerted`.
          No thresholds, caps, or cooldown numbers in the copy -- those
          are tuned in detectors.py/alerts.py and would go quietly stale
          here. */}
      {tiers.length > 0 && (
        <p className="tier-legend">
          <b>HIGH</b> is eligible for an immediate individual alert, subject to alert safeguards and
          your settings. <b>MEDIUM</b> is batched hourly and sent to users who choose aggressive
          alerts. <b>LOG</b> is lower-priority activity kept in Perch's journal; it is not sent as a
          user alert. These rates include qualifying journaled detections, not only delivered alerts.
        </p>
      )}

      {kinds.length === 0 && <p className="empty-state">Not enough history yet to report performance by signal type.</p>}
      {kinds.length > 0 && (
        <>
          <h2 className="view-section-title">By signal type</h2>
          <div className="stat-grid stat-grid-kinds">
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
          <h2 className="view-section-title">HIGH-tier track record</h2>
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
