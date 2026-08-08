import { useEffect, useMemo, useRef } from 'react'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { useReducedMotion, useFinePointer, useIsMobile } from '../hooks/usePrefs'
import './MarketField.css'

const SYMBOLS = [
  'AAPL','NVDA','TSLA','MSFT','AMZN','GOOG','META','SPY','QQQ','BTC','ETH',
  'AMD','NFLX','BE','IONQ','IWM','DIA','COIN','PLTR','SOFI','RIVN','SNAP',
]
const CHOSEN = 'NVDA'

function useField(count, isMobile) {
  return useMemo(() => {
    const rnd = (seed => () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed / 0x7fffffff
    })(7)
    const items = []
    for (let i = 0; i < count; i++) {
      const sym = SYMBOLS[i % SYMBOLS.length]
      let left, top
      if (isMobile) {
        // A compact radar instead of a huge scattered field -- a portrait
        // phone screen doesn't have the width to sell "thousands of tiny
        // points" the way a wide desktop field does, so symbols orbit a
        // center point instead. The 0.6 vertical compression roughly
        // corrects for percentage-of-axis vs. a typical portrait aspect,
        // so the ring reads as round rather than a tall ellipse.
        const angle = (i / count) * Math.PI * 2 + rnd() * 0.4
        const radius = 24 + rnd() * 18
        left = 50 + Math.cos(angle) * radius
        top = 50 + Math.sin(angle) * radius * 0.6
      } else {
        left = 4 + rnd() * 92
        top = 6 + rnd() * 88
      }
      items.push({
        sym,
        left,
        top,
        size: 0.72 + rnd() * 0.7,
        delay: rnd() * 4,
        isChosen: sym === CHOSEN && !items.some((it) => it.isChosen),
      })
    }
    return items
  }, [count, isMobile])
}

export default function MarketField() {
  const reduced = useReducedMotion()
  const fine = useFinePointer()
  const isMobile = useIsMobile()
  const count = isMobile ? 34 : 110
  const items = useField(count, isMobile)
  const rootRef = useRef(null)
  const fieldRef = useRef(null)

  useEffect(() => {
    if (!fine || reduced) return
    const field = fieldRef.current
    if (!field) return
    let raf
    const nodes = Array.from(field.querySelectorAll('.mf-sym'))
    const onMove = (e) => {
      const r = field.getBoundingClientRect()
      const mx = e.clientX - r.left
      const my = e.clientY - r.top
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = null
        nodes.forEach((n) => {
          const nr = n.getBoundingClientRect()
          const nx = nr.left - r.left + nr.width / 2
          const ny = nr.top - r.top + nr.height / 2
          const d = Math.hypot(mx - nx, my - ny)
          const boost = Math.max(0, 1 - d / 130)
          n.style.setProperty('--boost', boost.toFixed(2))
        })
      })
    }
    field.addEventListener('pointermove', onMove, { passive: true })
    return () => field.removeEventListener('pointermove', onMove)
  }, [fine, reduced])

  useEffect(() => {
    if (reduced) return
    const ctx = gsap.context(() => {
      const chosenEl = fieldRef.current.querySelector('.mf-sym.is-chosen')
      const others = fieldRef.current.querySelectorAll('.mf-sym:not(.is-chosen)')
      const label = rootRef.current.querySelector('.mf-caption')
      const kestrel = rootRef.current.querySelector('.mf-kestrel')
      const funnelNum = rootRef.current.querySelector('.mf-funnel-num')
      const funnelLbl = rootRef.current.querySelector('.mf-funnel-lbl')

      gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: 'top top',
          end: '+=140%',
          scrub: 0.6,
          pin: true,
        },
      })
        // The core story, told as numbers: thousands of events, filtered
        // down to a handful of anomalies, down to what's actually worth
        // seeing. Demo data -- clearly labeled, never claimed as live.
        .fromTo('.mf-funnel', { opacity: 0 }, { opacity: 1, duration: 0.15 }, 0)
        .fromTo(funnelNum, { innerText: 0 }, { innerText: 12481, duration: 0.001, snap: { innerText: 1 } }, 0)
        .set(funnelLbl, { innerText: 'MARKET EVENTS' }, 0)
        .to(funnelNum, { innerText: 47, duration: 0.3, snap: { innerText: 1 }, ease: 'power1.out' }, 0.25)
        .set(funnelLbl, { innerText: 'ANOMALIES' }, 0.25)
        .to(funnelNum, { innerText: 3, duration: 0.25, snap: { innerText: 1 }, ease: 'power1.out' }, 0.6)
        .set(funnelLbl, { innerText: 'WORTH YOUR ATTENTION' }, 0.6)
        .to('.mf-funnel', { opacity: 0, duration: 0.15 }, 0.82)
        // The kestrel moment: it drifts across from above, the field is
        // visible beneath it, then it dives toward the found signal and
        // dissolves into darkness -- Perch watching from above, made literal.
        .fromTo(kestrel, { opacity: 0, xPercent: -10, yPercent: -60, scale: 0.8 },
          { opacity: 0.9, xPercent: 20, yPercent: -20, scale: 1, duration: 1, ease: 'power1.inOut' }, 0)
        .to(kestrel, { xPercent: 55, yPercent: 8, scale: 0.55, opacity: 0, duration: 0.6, ease: 'power2.in' }, 0.55)
        .to(others, { opacity: 0, scale: 0.6, filter: 'blur(6px)', stagger: { each: 0.003, from: 'random' }, ease: 'power1.in' }, 0.15)
        .to(chosenEl, { left: '50%', top: '50%', scale: isMobile ? 2.6 : 3.4, duration: 1, ease: 'power2.inOut' }, 0.1)
        .to(chosenEl, { color: '#ff3b4e', duration: 0.3 }, 0.85)
        .fromTo(label, { opacity: 0, y: 14 }, { opacity: 1, y: 0, duration: 0.4 }, 0.88)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced, isMobile])

  return (
    <section className="market-field" ref={rootRef} id="field">
      <div className="mf-inner" ref={fieldRef}>
        <div className="mf-radar-ring" aria-hidden="true" />
        {items.map((it, i) => (
          <span
            key={i}
            className={`mf-sym${it.isChosen ? ' is-chosen' : ''}`}
            style={{
              left: `${it.left}%`,
              top: `${it.top}%`,
              fontSize: `${it.size}rem`,
              animationDelay: `${it.delay}s`,
            }}
          >
            {it.sym}
          </span>
        ))}
        {/* "The Windhover," tucked into a stoop -- same silhouette family as
            the hero (kestrelTexture.js) and the nav logo (PerchMark.jsx),
            wings pinned back for the dive rather than raised for the hover.
            The body+head path is identical to the hero's; only the wings
            and tail change for the tucked pose. */}
        <svg className="mf-kestrel" viewBox="0 0 260 260" aria-hidden="true">
          <g transform="rotate(38 130 130)">
            <path d="M96 118 C90 96 96 74 114 62 C110 48 116 32 132 26 C146 21 160 32 156 52
                     C148 68 134 74 126 96 C132 114 138 132 138 150 C138 166 130 180 116 184
                     C104 187 94 180 90 168 C82 178 70 186 56 188 C66 172 78 150 88 132
                     C90 126 93 122 96 118 Z" />
            <path opacity="0.88" d="M118 86 C112 72 114 58 122 50 C130 58 130 70 124 82
                     C122 85 120 86 118 86 Z" />
            <path d="M108 100 C100 118 96 140 100 160 C110 156 118 142 120 126
                     C124 140 132 152 142 158 C146 144 140 122 124 102 C118 96 112 96 108 100 Z" />
            <path d="M90 168 C82 190 80 208 86 220 C94 210 98 196 96 182 Z" />
          </g>
        </svg>
        <div className="mf-funnel" aria-hidden="true">
          <span className="mf-funnel-num">0</span>
          <span className="mf-funnel-lbl">MARKET EVENTS</span>
          <span className="demo-tag">Demo</span>
        </div>
        <div className="mf-caption">
          <span className="eyebrow"><span className="dot" /> SIGNAL DETECTED</span>
          <p>Something changed. Perch found it.</p>
        </div>
      </div>
    </section>
  )
}
