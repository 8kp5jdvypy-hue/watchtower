import { useState } from 'react'
import './MarketCoverage.css'

// Only "live" for capabilities the underlying scanner actually has today
// (equities + the ETFs already in its watch list). Everything else is
// honestly labeled coming soon rather than implied.
const CATEGORIES = [
  {
    key: 'stocks', label: 'Stocks', status: 'live',
    desc: 'Unusual price, volume, and range activity — measured against what\'s normal for that name, not a fixed threshold.',
    symbols: [{ s: 'NVDA', d: 'UNUSUAL' }, { s: 'TSLA', d: 'NORMAL' }, { s: 'AAPL', d: 'NORMAL' }, { s: 'MSFT', d: 'NORMAL' }],
  },
  {
    key: 'etfs', label: 'ETFs', status: 'live',
    // SPY/QQQ/IWM/USO are the actual ETF-type symbols in the live
    // watchlist (see tradebot/config.py's WATCHLIST) -- SMH/XLK aren't
    // monitored today, so showing them here as "live" would overstate
    // real coverage.
    desc: 'Sector and broad-market context, so a single name\'s move can be checked against what everything else is doing.',
    symbols: [{ s: 'SPY', d: 'NORMAL' }, { s: 'QQQ', d: 'NORMAL' }, { s: 'IWM', d: 'UNUSUAL' }, { s: 'USO', d: 'NORMAL' }],
  },
  {
    key: 'crypto', label: 'Crypto', status: 'soon',
    desc: '24/7 market intelligence, for a market that never closes.',
    symbols: [{ s: 'BTC', d: 'NORMAL' }, { s: 'ETH', d: 'NORMAL' }, { s: 'SOL', d: 'UNUSUAL' }],
  },
  {
    key: 'options', label: 'Options', status: 'soon',
    desc: 'Unusual derivatives activity, as a signal about where attention is concentrating.',
    symbols: [{ s: 'NVDA 25C', d: 'NORMAL' }, { s: 'SPY 0DTE', d: 'NORMAL' }, { s: 'TSLA 30P', d: 'UNUSUAL' }],
  },
  {
    key: 'market', label: 'Market', status: 'soon',
    desc: 'Broad market regime shifts — not just single names, but when conditions themselves change.',
    symbols: [{ s: 'BREADTH', d: 'NORMAL' }, { s: 'VOL REGIME', d: 'NORMAL' }, { s: 'CORRELATION', d: 'NORMAL' }],
  },
]

export default function MarketCoverage() {
  const [active, setActive] = useState('stocks')
  const cat = CATEGORIES.find((c) => c.key === active)

  return (
    <section className="market-coverage" id="coverage">
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> THE PERCH RADAR</span>
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

        <div className="mcv-radar" role="tabpanel">
          <p className="mcv-desc">{cat.desc}</p>

          <div className="mcv-field" key={cat.key}>
            {cat.symbols.map((sym, i) => (
              <span
                key={sym.s}
                className={`mcv-chip${sym.d === 'UNUSUAL' ? ' is-unusual' : ''}`}
                style={{ animationDelay: `${i * 0.05}s` }}
              >
                <span className="mcv-chip-sym">{sym.s}</span>
                <span className="mcv-chip-state">{sym.d}</span>
              </span>
            ))}
          </div>

          <span className="demo-tag">{cat.status === 'soon' ? 'Coming soon' : 'Simulated market activity'}</span>
        </div>
      </div>
    </section>
  )
}
