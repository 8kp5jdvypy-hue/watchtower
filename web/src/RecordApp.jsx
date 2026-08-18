import { useEffect, useState } from 'react'
import './RecordApp.css'

// Same hardcoded convention analytics.js/errorReporter.js already use on
// this site -- no env-based API URL mechanism exists here yet, and this
// page is public/unauthenticated so there's nothing environment-specific
// to configure.
const API_URL = 'https://api.perchmarkets.com'

const MIN_HISTORY_SAMPLE = 5 // mirrors tradebot.journal.MIN_HISTORY_SAMPLE

function pctSigned(value) {
  const sign = value >= 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function fmtSentAt(iso) {
  // Real send time, ET -- the same timezone every other Perch surface
  // (alert cards, the dashboard) already reports times in.
  const d = new Date(iso)
  const date = d.toLocaleDateString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric' })
  const time = d.toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return `${date}, ${time} ET`
}

function StatRow({ label, value }) {
  return (
    <div className="record-stat-row">
      <span className="record-stat-label">{label}</span>
      <span className="record-stat-value">{value}</span>
    </div>
  )
}

function AggregateBlock({ trackRecord }) {
  if (!trackRecord) {
    return (
      <p className="record-not-enough">
        Not enough sent alerts yet to report a win rate (need {MIN_HISTORY_SAMPLE}+ graded).
        The alerts below are everything sent so far.
      </p>
    )
  }
  return (
    <div className="record-stats">
      <StatRow label="Win rate" value={`${(trackRecord.hit_rate * 100).toFixed(1)}%`} />
      <StatRow label="Avg move (+30m)" value={pctSigned(trackRecord.avg_return_pct)} />
      <StatRow label="Sample size" value={`${trackRecord.sample_size} alerts`} />
      {trackRecord.significance && (
        <p className="record-significance">
          {trackRecord.significance.is_significant
            ? `Statistically ${trackRecord.hit_rate > 0.5 ? 'better' : 'worse'} than a coin flip (z=${trackRecord.significance.z_score.toFixed(2)}).`
            : `Not yet statistically different from a coin flip (z=${trackRecord.significance.z_score.toFixed(2)}) — still an early sample.`}
        </p>
      )}
    </div>
  )
}

function AlertRow({ alert }) {
  const graded = alert.tracked
  return (
    <div className="record-row">
      <div className="record-row-sent">{fmtSentAt(alert.sent_at)}</div>
      <div className="record-row-symbol">
        {alert.symbol}
        {alert.origin === 'screening' && <span className="record-badge">RADAR</span>}
      </div>
      <div className="record-row-direction">{alert.trend === 'up' ? 'BULLISH' : 'BEARISH'}</div>
      <div className="record-row-headline">{alert.headline}</div>
      <div className="record-row-outcome">
        {graded ? pctSigned(alert.return_pct) : <span className="record-pending">pending</span>}
      </div>
    </div>
  )
}

export default function RecordApp() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch(`${API_URL}/public/track-record`)
      .then((r) => {
        if (!r.ok) throw new Error(`status ${r.status}`)
        return r.json()
      })
      .then(setData)
      .catch(() => setError(true))
  }, [])

  return (
    <main className="record-page wrap">
      <header className="record-header">
        <div className="eyebrow">PERCH · TRACK RECORD</div>
        <h1>Every HIGH-tier alert Perch has sent.</h1>
        <p className="record-sub">
          Graded automatically at close. Nothing is edited or removed — a row appears the moment
          it's actually delivered, and stays exactly as it was sent.
        </p>
      </header>

      <section className="record-section">
        <h2>Methodology</h2>
        <p className="record-methodology">
          Perch scans a fixed watchlist intraday and pushes a HIGH-tier alert the moment a
          detector fires above a fixed threshold — every one below is journaled too, but only
          HIGH-tier alerts are shown here, and only the ones actually sent. "Continuation" is
          measured 30 minutes after each alert — the same horizon Perch's own dashboard and every
          alert card already use, so this page can never show a different number than what a
          subscriber saw at the time. A win rate below {MIN_HISTORY_SAMPLE} alerts isn't reported
          as a percentage — "not enough data to report" is the honest answer, not a number built
          on too little.
        </p>
      </section>

      <section className="record-section">
        <h2>The honest aggregate</h2>
        {error && <p className="record-error">Live data temporarily unavailable — try again shortly.</p>}
        {!error && !data && <p className="record-loading">Loading the record…</p>}
        {data && <AggregateBlock trackRecord={data.track_record} />}
      </section>

      <section className="record-section">
        <h2>Every alert</h2>
        {data && data.alerts.length === 0 && (
          <p className="record-not-enough">No alerts sent yet. Check back once the first one goes out.</p>
        )}
        {data && data.alerts.length > 0 && (
          <div className="record-table" role="table">
            <div className="record-row record-row-head" role="row">
              <div>Sent (ET)</div>
              <div>Symbol</div>
              <div>Direction</div>
              <div>Headline</div>
              <div>+30m</div>
            </div>
            {data.alerts.map((a) => (
              <AlertRow key={a.detection_id} alert={a} />
            ))}
          </div>
        )}
      </section>

      <footer className="record-footer">
        {data && `${data.alerts.length} alert${data.alerts.length === 1 ? '' : 's'}. Every one graded nightly. Unedited.`}
      </footer>
    </main>
  )
}
