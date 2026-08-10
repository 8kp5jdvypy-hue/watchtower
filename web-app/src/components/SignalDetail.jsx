import { useCallback, useEffect, useRef } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { explainContext } from '../signalContext'
import PerchMark from './PerchMark'
import './SignalDetail.css'

const KIND_LABELS = {
  level_break: 'Level break',
  rvol_spike: 'Volume spike',
  range_expansion: 'Range expansion',
  vwap_break: 'VWAP break',
  round_number_break: 'Round number',
  gap: 'Gap',
  relative_strength_break: 'Relative strength',
}
function kindLabel(kind) {
  return KIND_LABELS[kind] || kind.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function absoluteTime(tsUtc) {
  return new Date(tsUtc).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function pct(value) {
  return value == null ? '—' : `${(value * 100).toFixed(0)}%`
}

// A focused subset of what the landing page's demo AlertCard shows,
// built from real fields the API returns -- see tradebot/api/app.py's
// /signals/<id> and web-app/src/signalContext.js for where each piece
// of this actually comes from.
export default function SignalDetail({ id, onClose }) {
  const fetchDetail = useCallback(() => api.signalDetail(id), [id])
  const { data, error, loading } = useApiData(fetchDetail, [id])
  const dialogRef = useRef(null)
  const previouslyFocused = useRef(null)

  useEffect(() => {
    previouslyFocused.current = document.activeElement
    dialogRef.current?.focus()
    return () => {
      if (previouslyFocused.current instanceof HTMLElement) previouslyFocused.current.focus()
    }
  }, [])

  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab' || !dialogRef.current) return
      const focusable = dialogRef.current.querySelectorAll(
        'button, a[href], input, [tabindex]:not([tabindex="-1"])'
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const isHigh = data?.tier === 'high'
  const kinds = data?.kinds || []
  const contexts = data?.contexts || []

  return (
    <div className="signal-detail-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div
        className="signal-detail"
        role="dialog"
        aria-modal="true"
        aria-label={data ? `${data.symbol} signal detail` : 'Signal detail'}
        ref={dialogRef}
        tabIndex={-1}
      >
        <div className="sd-head">
          <span className="sd-head-id">
            <PerchMark size={14} state={isHigh ? 'alert' : 'confirmed'} accent={false} />
            <span className="sd-eyebrow">PERCH DETECTED</span>
          </span>
          <button type="button" className="sd-close" onClick={onClose} aria-label="Close signal detail">
            Close
          </button>
        </div>

        {loading && <p className="empty-state">Loading signal…</p>}
        {error && <p className="empty-state">Couldn't load this signal.</p>}

        {data && (
          <>
            <div className="sd-title-row">
              <span className={`sd-symbol ${data.trend === 'up' ? 'trend-up' : 'trend-down'}`}>
                {data.symbol}
                <span aria-hidden="true">{data.trend === 'up' ? '▲' : '▼'}</span>
              </span>
              <span className={`sd-tier sd-tier-${data.tier}`}>{data.tier}</span>
            </div>
            <p className="sd-headline">{data.headlines}</p>

            {(data.news_driven === true || data.news_driven === false) && (
              <div className="sd-flags">
                {data.news_driven ? (
                  <span className="sd-flag sd-flag-news">
                    News/event-driven{data.event_kind ? ` — ${data.event_kind}` : ''}
                  </span>
                ) : (
                  <span className="sd-flag">Clean technical setup — no known news event overlaps this</span>
                )}
                {data.alerted && <span className="sd-flag sd-flag-alerted">Sent as a live alert</span>}
              </div>
            )}
            {data.news_driven == null && data.alerted && (
              <div className="sd-flags">
                <span className="sd-flag sd-flag-alerted">Sent as a live alert</span>
              </div>
            )}

            <section className="sd-section">
              <h2 className="sd-section-title">Why Perch flagged this</h2>
              <ul className="sd-why-list">
                {kinds.map((kind, i) => (
                  <SignalWhyItem key={`${kind}-${i}`} kind={kind} context={contexts[i]} />
                ))}
              </ul>
            </section>

            <section className="sd-section">
              <h2 className="sd-section-title">Market context</h2>
              <div className="sd-context-grid">
                <div className="sd-context-item">
                  <span className="sd-context-label">Price at detection</span>
                  <span className="sd-context-value">{data.close != null ? `$${data.close.toFixed(2)}` : '—'}</span>
                </div>
                <div className="sd-context-item">
                  <span className="sd-context-label">ATR (14)</span>
                  <span className="sd-context-value">{data.atr14 != null ? data.atr14.toFixed(2) : '—'}</span>
                </div>
                <div className="sd-context-item">
                  <span className="sd-context-label">Score</span>
                  <span className="sd-context-value">{data.score != null ? data.score.toFixed(2) : '—'}</span>
                </div>
              </div>
            </section>

            <section className="sd-section">
              <h2 className="sd-section-title">Historical stats</h2>
              {data.history ? (
                <p className="sd-history">
                  <b>{data.history.sample_size}</b> historical observation{data.history.sample_size === 1 ? '' : 's'} of this
                  same setup · <b>{pct(data.history.continuation_rate)}</b> continued in the same direction within{' '}
                  {data.history.offset_min} min · avg follow-through{' '}
                  <b>{data.history.avg_return_pct.toFixed(2)}%</b>
                </p>
              ) : (
                <p className="sd-history sd-history-empty">
                  Not enough historical data yet for this exact setup to report a reliable base rate.
                </p>
              )}
            </section>

            <div className="sd-foot">
              <span>{absoluteTime(data.ts_utc)}</span>
              <span>{data.session}</span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function SignalWhyItem({ kind, context }) {
  return (
    <li className="sd-why-item">
      <span className="sd-kind-tag">{kindLabel(kind)}</span>
      <SignalWhyExplanation kind={kind} context={context} />
    </li>
  )
}

function SignalWhyExplanation({ kind, context }) {
  const { plain, technical } = explainContext(kind, context)
  return (
    <div className="sd-why-body">
      <p>{plain}</p>
      {technical.length > 0 && (
        <details className="sd-why-technical">
          <summary>Technical detail</summary>
          <dl>
            {technical.map(([label, value]) => (
              <div key={label} className="sd-why-technical-row">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  )
}
