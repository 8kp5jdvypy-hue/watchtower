import { useRef, useState } from 'react'
import PerchMark from './PerchMark'
import { useFinePointer, useReducedMotion } from '../hooks/usePrefs'
import './AlertCard.css'
import './PerchMark.css'

// data shape: { symbol, kind, detail, time, meterPct, meterVal, figs: [{label,val}],
// contexts: [{label,text}], why: [string] } -- one object per illustrative example,
// so the NVDA/AMD/SPY switcher can swap the whole card's content at once.
export default function AlertCard({ data, visible }) {
  const cardRef = useRef(null)
  const fine = useFinePointer()
  const reduced = useReducedMotion()
  const tiltActive = fine && !reduced
  const [open, setOpen] = useState(false)

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

  const { symbol, kind, detail, time, meterPct, meterVal, figs, contexts, why } = data

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
          {/* A small, static identity anchor -- this card only ever shows
              an already-confirmed signal, so there's no state to animate
              through here (that happens upstream, in AlertSequence). */}
          <PerchMark size={15} state="confirmed" accent={false} />
          <span className="eyebrow"><span className="dot" /> PERCH DETECTED</span>
        </span>
        <span className="demo-tag">Demo</span>
      </div>
      <div className="ac-body">
        <span className="ac-symbol">{symbol}</span>
        <span className="ac-kind">{kind}</span>
        <span className="ac-pulse" aria-hidden="true" />
        <p className="ac-detail">{detail}</p>
      </div>

      <div className="ac-expand" aria-hidden={!open}>
        <div className="ac-expand-in">
          <div className="ac-meter">
            <span className="ac-meter-label">Signal strength</span>
            <div className="ac-meter-track"><span className="ac-meter-fill" style={{ '--pct': meterPct }} /></div>
            <span className="ac-meter-val">{meterVal}</span>
          </div>
          <div className="ac-fig-row">
            {figs.map((f) => (
              <div className="ac-fig" key={f.label}><span>{f.label}</span><b>{f.val}</b></div>
            ))}
          </div>
          {contexts.map((c) => (
            <p className="ac-context" key={c.label}><b>{c.label} —</b> {c.text}</p>
          ))}
          <div className="ac-why">
            <span className="ac-why-label">Why Perch noticed</span>
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
