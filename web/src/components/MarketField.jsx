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

function useField(count) {
  return useMemo(() => {
    const rnd = (seed => () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed / 0x7fffffff
    })(7)
    const items = []
    for (let i = 0; i < count; i++) {
      const sym = SYMBOLS[i % SYMBOLS.length]
      items.push({
        sym,
        left: 4 + rnd() * 92,
        top: 6 + rnd() * 88,
        size: 0.72 + rnd() * 0.7,
        delay: rnd() * 4,
        isChosen: sym === CHOSEN && !items.some((it) => it.isChosen),
      })
    }
    return items
  }, [count])
}

export default function MarketField() {
  const reduced = useReducedMotion()
  const fine = useFinePointer()
  const isMobile = useIsMobile()
  const count = isMobile ? 46 : 110
  const items = useField(count)
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

      gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: 'top top',
          end: '+=140%',
          scrub: 0.6,
          pin: true,
        },
      })
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
        <div className="mf-caption">
          <span className="eyebrow"><span className="dot" /> SIGNAL DETECTED</span>
          <p>Something changed. Perch found it.</p>
        </div>
      </div>
    </section>
  )
}
