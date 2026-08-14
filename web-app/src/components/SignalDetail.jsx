import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { explainContext } from '../signalContext'
import { SMALL_SAMPLE_THRESHOLD, interpretHistory } from '../signalHistory'
import { kindLabel } from '../kindLabels'
import PerchMark from './PerchMark'
import './SignalDetail.css'

function absoluteTime(tsUtc) {
  return new Date(tsUtc).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function pct(value) {
  return value == null ? '—' : `${(value * 100).toFixed(0)}%`
}

// The fixed checkpoints journal.backfill_marks() ever writes (see
// tradebot/journal.py's OUTCOME_OFFSETS_MIN) -- a stable UI constant, not
// detection logic. "At session close" is handled separately below since
// it isn't a fixed offset.
const AFTER_DETECTION_OFFSETS = [15, 30, 60]

// One row per possible checkpoint, whether or not it's backfilled yet --
// a signal detected late in the session may never get all of them, and
// that's normal, not an error (see /signals/<id>'s marks field: empty
// until journal.backfill_marks() runs, once, at session end). Missing
// rows render a resolution estimate rather than a bare "Pending", so the
// section reads as "still developing on this schedule," not "broken."
function afterDetectionRows(marks) {
  return [
    ...AFTER_DETECTION_OFFSETS.map((offsetMin) => ({
      key: `offset-${offsetMin}`,
      label: `+${offsetMin} min after detection`,
      offsetMin,
      mark: marks?.find((m) => m.offset_min === offsetMin && !m.at_close),
    })),
    { key: 'close', label: 'At session close', offsetMin: null, mark: marks?.find((m) => m.at_close) },
  ]
}

// backfill_marks() only ever runs once, at session close, for every
// checkpoint including +15min -- there's no incremental resolution (see
// tradebot/journal.py's backfill_marks()). So a checkpoint whose target
// time hasn't arrived yet can honestly be given that time ("resolves
// ~2:47 PM"), but once the target time has passed, the only honest thing
// left to say is that it's waiting on the once-daily close batch, same
// as the close row itself -- showing the already-elapsed target time
// there would look exactly as broken as a bare "Pending" did.
function pendingResolutionLabel(offsetMin, tsUtc) {
  if (offsetMin == null) return 'Resolves after session close'
  const targetMs = new Date(tsUtc).getTime() + offsetMin * 60 * 1000
  if (Date.now() < targetMs) {
    const time = new Date(targetMs).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
    return `Resolves ~${time}`
  }
  return 'Resolves after session close'
}

// Matches .signal-detail's CSS transition duration -- see sd-panel-in/
// sd-sheet-in. Kept in one place so the JS unmount delay and the CSS
// animation length can't silently drift apart.
const CLOSE_ANIMATION_MS = 200

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

// A focused subset of what the landing page's demo AlertCard shows,
// built from real fields the API returns -- see tradebot/api/app.py's
// /signals/<id> and web-app/src/signalContext.js for where each piece
// of this actually comes from.
export default function SignalDetail({ id, onClose }) {
  const [retryKey, setRetryKey] = useState(0)
  const fetchDetail = useCallback(() => api.signalDetail(id), [id])
  const { data, error, loading } = useApiData(fetchDetail, [id, retryKey])
  const dialogRef = useRef(null)
  const previouslyFocused = useRef(null)
  const [closing, setClosing] = useState(false)

  // Closing plays the reverse of the entrance transition instead of the
  // panel just vanishing -- an instant cut reads cheap for something
  // meant to feel like closing a report, not a debug panel. Skipped
  // entirely for reduced-motion, where there's nothing to wait out.
  const requestClose = useCallback(() => {
    if (prefersReducedMotion()) {
      onClose()
      return
    }
    setClosing(true)
  }, [onClose])

  useEffect(() => {
    if (!closing) return
    const t = setTimeout(onClose, CLOSE_ANIMATION_MS)
    return () => clearTimeout(t)
  }, [closing, onClose])

  useEffect(() => {
    previouslyFocused.current = document.activeElement
    dialogRef.current?.focus()
    // Background content stays reachable by scroll/gesture behind a
    // fixed overlay unless the page itself is locked -- easy to miss
    // on mobile, where the sheet covers less than the full viewport.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
      if (previouslyFocused.current instanceof HTMLElement) previouslyFocused.current.focus()
    }
  }, [])

  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === 'Escape') {
        requestClose()
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
  }, [requestClose])

  const isHigh = data?.tier === 'high'
  const kinds = data?.kinds || []
  const contexts = data?.contexts || []

  // Portaled to <body>: rendered in place, this fixed overlay sits inside
  // .view's `z-index: 1` stacking context (Views.css), so its z-index: 50
  // competes as a 1 -- the tab bar (z 2) and mobile nav (z 10) painted on
  // top of the open dialog and stayed clickable. The portal opts it out of
  // every ancestor stacking context; focus trap, scroll lock, and Escape
  // handling are document-level already and don't care where it mounts.
  return createPortal(
    <div
      className={`signal-detail-overlay${closing ? ' is-closing' : ''}`}
      onMouseDown={(e) => { if (e.target === e.currentTarget) requestClose() }}
    >
      <div
        className={`signal-detail${closing ? ' is-closing' : ''}`}
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
          <button type="button" className="sd-close" onClick={requestClose} aria-label="Close signal detail">
            Close
          </button>
        </div>

        {loading && <SignalDetailSkeleton />}

        {error && (
          <div className="sd-error" role="alert">
            <p>Perch couldn't load this signal.</p>
            <button type="button" className="sd-retry" onClick={() => setRetryKey((k) => k + 1)}>
              Try again
            </button>
          </div>
        )}

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

            {(data.news_driven === true || data.news_driven === false || data.alerted || data.origin === 'screening') && (
              <div className="sd-flags">
                {data.news_driven === true && (
                  <span className="sd-flag sd-flag-news">
                    News/event-driven{data.event_kind ? ` — ${data.event_kind}` : ''}
                  </span>
                )}
                {data.news_driven === false && (
                  <span className="sd-flag">Clean technical setup — no known news event overlaps this</span>
                )}
                {data.alerted && <span className="sd-flag sd-flag-alerted">Sent as a live alert</span>}
                {/* Not on the subscriber's watchlist -- broad_scan promoted it
                    in for today's session. See
                    docs/broad-scan-honesty-proposal.md finding (a). */}
                {data.origin === 'screening' && (
                  <span className="sd-flag sd-flag-radar">Radar — not on your watchlist, flagged by today's daily screen</span>
                )}
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
              <h2 className="sd-section-title">Historical stats</h2>
              {data.history ? (
                <>
                  <p className="sd-history">
                    <b>{data.history.sample_size}</b> historical observation{data.history.sample_size === 1 ? '' : 's'} of this
                    same setup · <b>{pct(data.history.continuation_rate)}</b> continued in the same direction within{' '}
                    {data.history.offset_min} min · avg follow-through{' '}
                    <b>{data.history.avg_return_pct.toFixed(2)}%</b>
                    {data.history.avg_return_atr != null && (
                      <span className="sd-history-atr"> (≈{data.history.avg_return_atr.toFixed(2)}× ATR)</span>
                    )}
                  </p>
                  {data.history.sample_size < SMALL_SAMPLE_THRESHOLD && (
                    <span className="sd-small-sample">Small sample — treat as weak evidence</span>
                  )}
                  <p className="sd-history-interpretation">{interpretHistory(data.history, data.trend)}</p>
                </>
              ) : (
                <p className="sd-history sd-history-empty">
                  Not enough historical data yet for this exact setup to report a reliable base rate.
                </p>
              )}
            </section>

            {/* % is derived strictly against `close` (price at detection,
                the same field Market context shows below) -- never a live
                quote. Real backfilled outcomes only, see afterDetectionRows(). */}
            {data.close != null && (
              <section className="sd-section">
                <h2 className="sd-section-title">After detection</h2>
                <ul className="sd-marks-list">
                  {afterDetectionRows(data.marks).map(({ key, label, offsetMin, mark }) => (
                    <li className="sd-mark-row" key={key}>
                      <span className="sd-mark-label">{label}</span>
                      {mark ? (
                        <span className={`sd-mark-value ${mark.price >= data.close ? 'trend-up' : 'trend-down'}`}>
                          ${mark.price.toFixed(2)} ({(((mark.price - data.close) / data.close) * 100).toFixed(2)}%)
                        </span>
                      ) : (
                        <span className="sd-mark-pending">{pendingResolutionLabel(offsetMin, data.ts_utc)}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="sd-section">
              <h2 className="sd-section-title">Market context</h2>
              <div className="sd-context-grid">
                <div className="sd-context-item">
                  <span className="sd-context-label">Price at detection</span>
                  <span className="sd-context-value">{data.close != null ? `$${data.close.toFixed(2)}` : '—'}</span>
                </div>
                <div className="sd-context-item">
                  <span className="sd-context-label">Typical range (ATR-14)</span>
                  <span className="sd-context-value">{data.atr14 != null ? `$${data.atr14.toFixed(2)}` : '—'}</span>
                </div>
              </div>
              {data.score != null && (
                <details className="sd-why-technical sd-tech-details">
                  <summary>Technical details</summary>
                  <dl>
                    <div className="sd-why-technical-row">
                      <dt>Signal score</dt>
                      <dd>{data.score.toFixed(2)}</dd>
                    </div>
                  </dl>
                </details>
              )}
            </section>

            <div className="sd-foot">
              <span>{absoluteTime(data.ts_utc)}</span>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body
  )
}

function SignalDetailSkeleton() {
  return (
    <div className="sd-skeleton" aria-live="polite" aria-busy="true">
      <span className="sr-only">Loading signal…</span>
      <div className="sd-skel-line sd-skel-title" aria-hidden="true" />
      <div className="sd-skel-line sd-skel-sub" aria-hidden="true" />
      <div className="sd-skel-block" aria-hidden="true" />
      <div className="sd-skel-block sd-skel-block-sm" aria-hidden="true" />
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
