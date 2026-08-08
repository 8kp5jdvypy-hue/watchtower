import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { useReducedMotion } from '../hooks/usePrefs'
import './MarketContext.css'

const ROWS = [
  { sym: 'NVDA', name: 'The name', chg: '+4.2%', hero: true },
  { sym: 'SOXX', name: 'Its sector — semiconductors', chg: '+1.8%' },
  { sym: 'XLK', name: 'Broader technology', chg: '+0.9%' },
  { sym: 'SPY', name: 'The overall market', chg: '+0.3%' },
]

export default function MarketContext() {
  const reduced = useReducedMotion()
  const rootRef = useRef(null)

  useEffect(() => {
    if (reduced || !rootRef.current) return
    const ctx = gsap.context(() => {
      gsap.from('.mc-row', {
        opacity: 0, x: -14, duration: 0.6, stagger: 0.1, ease: 'power2.out',
        scrollTrigger: { trigger: rootRef.current, start: 'top 75%' },
      })
      gsap.from('.mc-obs', {
        opacity: 0, y: 16, duration: 0.6, delay: 0.3, ease: 'power2.out',
        scrollTrigger: { trigger: rootRef.current, start: 'top 75%' },
      })
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  return (
    <section className="market-context" ref={rootRef}>
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> CONTEXT, NOT JUST PRICE</span>
          <h2>A move only means something in context.</h2>
          <p className="mc-sub">Perch doesn't just see "NVDA is up." It checks that against the sector, the broader market, and what's normal — before it decides the move is worth your attention. <span className="demo-tag">Demo</span></p>
        </div>

        <div className="mc-table">
          {ROWS.map((r) => (
            <div className={`mc-row${r.hero ? ' is-hero' : ''}`} key={r.sym}>
              <span className="mc-sym">{r.sym}</span>
              <span className="mc-name">{r.name}</span>
              <span className="mc-chg">{r.chg}</span>
            </div>
          ))}
        </div>

        <div className="mc-obs">
          <span className="eyebrow"><span className="dot" /> PERCH OBSERVATION</span>
          <p>NVDA is significantly outperforming its sector and the broader market — not just moving with everything else today.</p>
        </div>
      </div>
    </section>
  )
}
