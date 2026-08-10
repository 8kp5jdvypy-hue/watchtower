import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import AlertCard from './AlertCard'
import PerchMark from './PerchMark'
import { useReducedMotion } from '../hooks/usePrefs'
import { SIGNUP_URL } from '../config'
import './AlertReveal.css'

// Three compact, illustrative examples -- the point isn't more sections,
// it's showing Perch watches broadly with one good interaction. All figures
// are demo data, clearly labeled; none of this is a real-time claim.
const EXAMPLES = {
  NVDA: {
    tab: 'NVDA', price: '+6.4%', priceTime: '12:14 PM ET',
    data: {
      symbol: 'NVDA', kind: 'Unusual volume', time: '12:14 PM ET',
      detail: 'Volume is significantly above its recent average, with price expanding outside the normal intraday range.',
      meterPct: '82%', meterVal: '5.0 / 6',
      figs: [{ label: 'Volume', val: '3.1× avg' }, { label: 'Rel. strength', val: '+3.9% vs SOXX' }],
      contexts: [
        { label: 'Market context', text: 'Outperforming its sector and the broader market today, not just moving with everything else.' },
        { label: 'Historical context', text: 'Volume expansions of this size in this name have preceded continued directional moves, demo data.' },
      ],
      why: [
        'Volume is 3.1× the 20-day average for this time of day',
        'Price range expanded to 2.3 ATR, well outside its normal band',
        'The move diverges from its sector and the broader market today',
      ],
    },
  },
  AMD: {
    tab: 'AMD', price: '+1.1%', priceTime: '1:32 PM ET',
    data: {
      symbol: 'AMD', kind: 'Volume anomaly', time: '1:32 PM ET',
      detail: 'Trading volume spiked well above normal levels with no corresponding move in the sector yet.',
      meterPct: '68%', meterVal: '4.1 / 6',
      figs: [{ label: 'Volume', val: '2.6× avg' }, { label: 'Rel. strength', val: '+1.1% vs SOXX' }],
      contexts: [
        { label: 'Market context', text: "Volume is unusual, but price hasn't followed yet — a divergence worth watching, not acting on." },
        { label: 'Historical context', text: 'Volume-only anomalies like this are a lower-confidence signal than a combined volume+price move, demo data.' },
      ],
      why: [
        'Volume is 2.6× the 20-day average with price roughly flat',
        'No corresponding move yet in the sector or broader market',
        'Historically a lower-confidence signal than a combined price+volume move',
      ],
    },
  },
  SPY: {
    tab: 'SPY', price: '+0.9%', priceTime: '3:47 PM ET',
    data: {
      symbol: 'SPY', kind: 'Market-wide move', time: '3:47 PM ET',
      detail: 'Broad-based volatility expansion across the index, not concentrated in any single sector.',
      meterPct: '74%', meterVal: '4.5 / 6',
      figs: [{ label: 'Breadth', val: '91% advancing' }, { label: 'Volatility', val: '+14% vs avg' }],
      contexts: [
        { label: 'Market context', text: "This isn't one stock — it's the whole market moving together, which changes what the move means." },
        { label: 'Historical context', text: 'Broad-based moves like this tend to line up with macro catalysts, demo data.' },
      ],
      why: [
        'Advancing issues significantly outweigh declining ones',
        'Volatility expanded across the index, not one sector',
        'The move is broad-based rather than concentrated in a single name',
      ],
    },
  },
}
const ORDER = ['NVDA', 'AMD', 'SPY']

export default function AlertReveal() {
  const reduced = useReducedMotion()
  const [active, setActive] = useState('NVDA')
  const [shown, setShown] = useState(false)
  const rootRef = useRef(null)
  const ex = EXAMPLES[active]

  useEffect(() => {
    if (!rootRef.current || !('IntersectionObserver' in window)) { setShown(true); return }
    const io = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setShown(true); io.disconnect() }
    }, { threshold: 0.3 })
    io.observe(rootRef.current)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    if (reduced || !shown || !rootRef.current) return
    const ctx = gsap.context(() => {
      const tl = gsap.timeline()
      // The card doesn't just fade on -- its border draws in first, like
      // a piece of hardware powering on, then its content fills in.
      tl.fromTo('.ar-card-frame', { opacity: 0, scale: 0.97 }, { opacity: 1, scale: 1, duration: 0.5, ease: 'power2.out' })
        .fromTo('.alert-card', { opacity: 0 }, { opacity: 1, duration: 0.01 }, 0)
        .fromTo('.ar-phone', { opacity: 0, y: 24, rotate: -2 }, { opacity: 1, y: 0, rotate: 0, duration: 0.6, ease: 'power3.out' }, 0.15)
        .fromTo('.ar-phone-notif', { opacity: 0, y: -14, scale: 0.94 }, { opacity: 1, y: 0, scale: 1, duration: 0.45, ease: 'back.out(1.6)' }, 0.5)
        .fromTo('.ar-close', { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.4 }, 0.7)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced, shown])

  return (
    <section className={`alert-reveal${shown ? ' is-shown' : ''}`} ref={rootRef} id="demo">
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> THE ALERT</span>
          <h2>This is what lands in your feed.</h2>
          <p className="ar-sub">Perch watches broadly — try a different name. <span className="demo-tag">Illustrative, not live data</span></p>
        </div>

        <div className="ar-tabs" role="tablist" aria-label="Example signal">
          {ORDER.map((key) => (
            <button
              key={key}
              role="tab"
              aria-selected={active === key}
              className={`ar-tab${active === key ? ' is-active' : ''}`}
              onClick={() => setActive(key)}
              data-cursor="link"
            >
              {key}
            </button>
          ))}
        </div>

        <div className="ar-grid">
          <div className="ar-card-frame">
            <AlertCard data={ex.data} visible={shown} />
          </div>

          {/* A premium mobile-notification mockup -- not Telegram's own UI
              or logo, just a generic, restrained "this is what it feels
              like to receive this" presentation, since that's the actual
              delivery channel Perch is built around. */}
          <div className="ar-phone" aria-hidden="true">
            <div className="ar-phone-frame">
              <div className="ar-phone-notch" />
              <div className="ar-phone-screen">
                <div className="ar-phone-notif">
                  <div className="ar-notif-head">
                    <PerchMark size={16} variant="ink" accent={false} />
                    <span className="ar-notif-brand">Perch</span>
                    <span className="ar-notif-time">now</span>
                  </div>
                  <div className="ar-notif-title">Signal detected — {ex.data.symbol}</div>
                  <div className="ar-notif-body">
                    {ex.data.kind}. {ex.data.figs[0].label} {ex.data.figs[0].val}, price {ex.price}.
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <p className="ar-close">
          You don't have to watch everything. Perch does.{' '}
          <a href={SIGNUP_URL} data-cursor="link">Sign up →</a>
        </p>
      </div>
    </section>
  )
}
