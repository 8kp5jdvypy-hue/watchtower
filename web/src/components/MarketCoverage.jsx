import { useState } from 'react'
import './MarketCoverage.css'

// Only "live" for capabilities the underlying scanner actually has today
// (equities + the ETFs already in its watch list). Everything else is
// honestly labeled coming soon rather than implied.
const CATEGORIES = [
  { key: 'stocks', label: 'Stocks', status: 'live', desc: 'Unusual price, volume, and range activity — measured against what\'s normal for that name, not a fixed threshold.' },
  { key: 'etfs', label: 'ETFs', status: 'live', desc: 'Sector and broad-market context, so a single name\'s move can be checked against what everything else is doing.' },
  { key: 'crypto', label: 'Crypto', status: 'soon', desc: '24/7 market intelligence, for a market that never closes.' },
  { key: 'options', label: 'Options', status: 'soon', desc: 'Unusual derivatives activity, as a signal about where attention is concentrating.' },
  { key: 'market', label: 'Market', status: 'soon', desc: 'Broad market regime shifts — not just single names, but when conditions themselves change.' },
]

export default function MarketCoverage() {
  const [active, setActive] = useState('stocks')
  const cat = CATEGORIES.find((c) => c.key === active)

  return (
    <section className="market-coverage" id="coverage">
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> COVERAGE</span>
          <h2>One market isn't enough.</h2>
        </div>

        <div className="mcv-tabs" role="tablist">
          {CATEGORIES.map((c) => (
            <button
              key={c.key}
              role="tab"
              aria-selected={active === c.key}
              className={`mcv-tab${active === c.key ? ' is-active' : ''}`}
              onClick={() => setActive(c.key)}
              data-cursor="link"
            >
              {c.label}
              {c.status === 'soon' && <span className="mcv-soon">Soon</span>}
            </button>
          ))}
        </div>

        <div className="mcv-panel" role="tabpanel">
          <p>{cat.desc}</p>
          {cat.status === 'soon' && <span className="demo-tag">Coming soon</span>}
        </div>
      </div>
    </section>
  )
}
