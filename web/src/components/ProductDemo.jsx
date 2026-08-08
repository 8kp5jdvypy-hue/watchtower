import { useState } from 'react'
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
                onMouseEnter={() => setActive(a.sym)}
                onMouseLeave={() => setActive((cur) => (cur === a.sym ? null : cur))}
                onFocus={() => setActive(a.sym)}
                onBlur={() => setActive((cur) => (cur === a.sym ? null : cur))}
                data-cursor="data"
              >
                <span className="pd-sym">{a.sym}</span>
                <span className="pd-state">{a.state}</span>
              </button>
            ))}
          </div>
          <div className="pd-stage">
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
