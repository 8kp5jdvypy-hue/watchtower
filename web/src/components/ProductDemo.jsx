import { useEffect, useRef, useState } from 'react'
import AlertCard from './AlertCard'
import './ProductDemo.css'

const ASSETS = [
  { sym: 'SPY', state: 'NORMAL' },
  { sym: 'AAPL', state: 'NORMAL' },
  { sym: 'MSFT', state: 'NORMAL' },
  { sym: 'TSLA', state: 'NORMAL' },
  { sym: 'NVDA', state: 'UNUSUAL' },
]

export default function ProductDemo() {
  const [active, setActive] = useState(null)
  const clearTimer = useRef(null)

  useEffect(() => () => clearTimeout(clearTimer.current), [])

  // The row and the card it reveals sit in separate grid columns, so the
  // mouse has to cross a gap to reach the card's own "View signal" button.
  // Clearing `active` immediately on mouseleave would hide (and stop
  // accepting clicks on) the card mid-transit -- a short grace period, like
  // any hover menu, gives the pointer time to land on what it's reaching for.
  function activate(sym) {
    clearTimeout(clearTimer.current)
    setActive(sym)
  }
  function scheduleClear(sym) {
    clearTimeout(clearTimer.current)
    clearTimer.current = setTimeout(() => {
      setActive((cur) => (cur === sym ? null : cur))
    }, 220)
  }

  // Touch devices don't fire hover -- a tap has to be able to both open
  // and close the same row, since there's no mouseleave to fall back on.
  // Hover-capable devices already get this from onMouseEnter/onMouseLeave;
  // toggling again on their click would immediately close what hover just
  // opened, so this only acts where hover doesn't exist.
  function onTap(sym) {
    if (window.matchMedia('(hover: hover)').matches) return
    setActive((cur) => (cur === sym ? null : sym))
  }

  return (
    <section className="product-demo" id="demo">
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> THE INTERFACE</span>
          <h2>Hover a name.</h2>
          <p className="pd-sub">This is what Perch looks like when it finds something. <span className="demo-tag">Demo data</span></p>
        </div>

        <div className="pd-grid">
          <div className="pd-list">
            {ASSETS.map((a) => (
              <button
                key={a.sym}
                className={`pd-row${a.state === 'UNUSUAL' ? ' is-unusual' : ''}${active === a.sym ? ' is-active' : ''}`}
                onMouseEnter={() => activate(a.sym)}
                onMouseLeave={() => scheduleClear(a.sym)}
                onFocus={() => activate(a.sym)}
                onBlur={() => scheduleClear(a.sym)}
                onClick={() => onTap(a.sym)}
                data-cursor="data"
              >
                <span className="pd-sym">{a.sym}</span>
                <span className="pd-state">{a.state}</span>
              </button>
            ))}
          </div>
          <div
            className="pd-stage"
            onMouseEnter={() => activate('NVDA')}
            onMouseLeave={() => scheduleClear('NVDA')}
          >
            <AlertCard
              symbol="NVDA"
              kind="Unusual volume"
              detail="Volume is significantly above its recent average, with price expanding outside the normal intraday range."
              time="12:14 PM ET"
              visible={active === 'NVDA'}
            />
            {!active && <p className="pd-hint">Hover NVDA to see a signal fire.</p>}
          </div>
        </div>
      </div>
    </section>
  )
}
