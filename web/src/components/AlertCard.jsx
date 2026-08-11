import { useEffect, useRef, useState } from 'react'
import PerchMark from './PerchMark'
import { useFinePointer, useReducedMotion } from '../hooks/usePrefs'
import './AlertCard.css'
import './PerchMark.css'

// data shape: { symbol, kind, tier, headline, technical, time, figs: [{label,val}],
// contexts: [{label,text}], history: {sampleSize,continuationRate,offsetMin,avgReturnPct},
// why: [string] } -- one object per illustrative example, so the NVDA/AMD/SPY
// switcher can swap the whole card's content at once.

// Mirrors web-app/src/signalHistory.js's SMALL_SAMPLE_THRESHOLD and
// interpretHistory() exactly -- same threshold, same decent/mixed/weak
// wording -- so the demo teaches the same honesty the real product shows,
// not a simplified version of it. Self-contained on purpose (Tier 1.4):
// this is a copy, not a shared import across the two separate projects.
const SMALL_SAMPLE_THRESHOLD = 10

function interpretHistory({ continuationRate, avgReturnPct }) {
  const nearZero = Math.abs(avgReturnPct) < 0.1
  if (continuationRate < 0.45 || (avgReturnPct < 0 && !nearZero)) {
    return 'Historically weak follow-through for this setup.'
  }
  if (continuationRate >= 0.6 && avgReturnPct > 0 && !nearZero) {
    return 'Historically decent follow-through for this setup.'
  }
  return 'Historically mixed results for this setup.'
}

function pct(value) {
  return `${Math.round(value * 100)}%`
}

export default function AlertCard({ data, visible }) {
  const cardRef = useRef(null)
  const fine = useFinePointer()
  const reduced = useReducedMotion()
  const tiltActive = fine && !reduced
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    function onKeyDown(e) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open])

  function onMove(e) {
    if (!tiltActive || !cardRef.current) return
    const r = cardRef.current.getBoundingClientRect()
    const px = (e.clientX - r.left) / r.width
    const py = (e.clientY - r.top) / r.height
    cardRef.current.style.setProperty('--rx', ((0.5 - py) * 6).toFixed(2))
    cardRef.current.style.setProperty('--ry', ((px - 0.5) * 8).toFixed(2))
    cardRef.current.style.setProperty('--mx', `${(px * 100).toFixed(1)}%`)
    cardRef.current.style.setProperty('--my', `${(py * 100).toFixed(1)}%`)
  }
  function onLeave() {
    if (!cardRef.current) return
    cardRef.current.style.setProperty('--rx', 0)
    cardRef.current.style.setProperty('--ry', 0)
  }

  const { symbol, kind, tier, headline, technical, time, figs, contexts, history, why } = data
  const isHigh = tier === 'high'

  return (
    <div
      className={`alert-card${visible ? ' is-visible' : ''}${open ? ' is-open' : ''}`}
      data-cursor="data"
      ref={cardRef}
      onPointerMove={onMove}
      onPointerLeave={onLeave}
    >
      <div className="ac-head">
        <span className="ac-head-id">
          {/* Kind-label eyebrow, not a repeated "PERCH DETECTED" -- matches
              the shipped SignalCard.jsx exactly (see Tier 1.3). */}
          <PerchMark size={15} state={isHigh ? 'alert' : 'confirmed'} accent={false} />
          <span className="ac-kind-eyebrow">{kind}</span>
        </span>
        <span className="ac-head-badges">
          <span className={`ac-tier ac-tier-${tier}`}>{tier}</span>
          <span className="demo-tag">Demo</span>
        </span>
      </div>
      <div className="ac-body">
        <span className="ac-symbol">{symbol}</span>
        <span className="ac-pulse" aria-hidden="true" />
        <p className="ac-headline">{headline}</p>
        <p className="ac-technical">{technical}</p>
      </div>

      <div className="ac-expand" aria-hidden={!open}>
        <div className="ac-expand-in">
          <div className="ac-fig-row">
            {figs.map((f) => (
              <div className="ac-fig" key={f.label}><span>{f.label}</span><b>{f.val}</b></div>
            ))}
          </div>
          {contexts.map((c) => (
            <p className="ac-context" key={c.label}><b>{c.label} —</b> {c.text}</p>
          ))}
          <div className="ac-history">
            <span className="ac-history-label">Historical stats</span>
            <p className="ac-history-line">
              <b>{history.sampleSize}</b> historical observation{history.sampleSize === 1 ? '' : 's'} of this
              same setup · <b>{pct(history.continuationRate)}</b> continued in the same direction within{' '}
              {history.offsetMin} min · avg follow-through <b>{history.avgReturnPct.toFixed(2)}%</b>
            </p>
            {history.sampleSize < SMALL_SAMPLE_THRESHOLD && (
              <span className="ac-small-sample">Small sample — treat as weak evidence</span>
            )}
            <p className="ac-history-interpretation">{interpretHistory(history)}</p>
          </div>
          <div className="ac-why">
            <span className="ac-why-label">Why Perch flagged this</span>
            <ul>
              {why.map((w) => <li key={w}>{w}</li>)}
            </ul>
          </div>
        </div>
      </div>

      <div className="ac-foot">
        <span className="ac-time">{time}</span>
        <button className="ac-view" data-cursor="link" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? 'Close' : 'View signal'}
        </button>
      </div>
    </div>
  )
}
