import { useRef, useState } from 'react'
import { useFinePointer, useReducedMotion } from '../hooks/usePrefs'
import './AlertCard.css'

const WHY = [
  'Volume is 3.1× the 20-day average for this time of day',
  'Price range expanded to 2.3 ATR, well outside its normal band',
  'The move diverges from its sector and the broader market today',
]

export default function AlertCard({ symbol, kind = 'Unusual volume', detail, time, visible }) {
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

  return (
    <div
      className={`alert-card${visible ? ' is-visible' : ''}${open ? ' is-open' : ''}`}
      data-cursor="data"
      ref={cardRef}
      onPointerMove={onMove}
      onPointerLeave={onLeave}
    >
      <div className="ac-head">
        <span className="eyebrow"><span className="dot" /> PERCH DETECTED</span>
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
            <div className="ac-meter-track"><span className="ac-meter-fill" style={{ '--pct': '82%' }} /></div>
            <span className="ac-meter-val">5.0 / 6</span>
          </div>
          <div className="ac-fig-row">
            <div className="ac-fig"><span>Volume</span><b>3.1× avg</b></div>
            <div className="ac-fig"><span>Rel. strength</span><b>+3.9% vs SOXX</b></div>
          </div>
          <p className="ac-context"><b>Market context —</b> Outperforming its sector and the broader market today, not just moving with everything else.</p>
          <p className="ac-context"><b>Historical context —</b> Volume expansions of this size in this name have preceded continued directional moves, demo data.</p>
          <div className="ac-why">
            <span className="ac-why-label">Why Perch noticed</span>
            <ul>
              {WHY.map((w) => <li key={w}>{w}</li>)}
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
