import { useEffect, useMemo, useRef } from 'react'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { buildCandles } from '../scenes/candleData'
import { useReducedMotion } from '../hooks/usePrefs'
import './SignalVisualization.css'

const W = 640, H = 260, PAD = 0.14

function toPath({ ohlc, trig, n }) {
  const vals = ohlc.flatMap((d) => [d.hi, d.lo])
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const span = (hi - lo) || 1
  const py = (v) => (1 - (PAD + ((v - lo) / span) * (1 - 2 * PAD))) * H
  const step = W / n
  const bw = step * 0.56
  const bars = ohlc.map((d) => {
    const cx = d.i * step + step / 2
    const up = d.c >= d.o
    const bodyTop = py(Math.max(d.o, d.c))
    const bodyH = Math.max(1.4, Math.abs(py(d.o) - py(d.c)))
    return { cx, bw, wickY1: py(d.hi), wickY2: py(d.lo), bodyTop, bodyH, alert: d.i >= trig }
  })
  const trigX = trig * step + step / 2
  return { bars, trigX }
}

const STATS = [
  { label: 'PRICE', value: '$146.82', delta: '+6.4%' },
  { label: 'VOLUME', value: '3.1×', delta: 'avg' },
  { label: 'VOLATILITY', value: '2.3 ATR', delta: 'expansion' },
  { label: 'TIME', value: '12:14 PM', delta: 'ET' },
]

export default function SignalVisualization() {
  const reduced = useReducedMotion()
  const rootRef = useRef(null)
  const data = useMemo(() => toPath(buildCandles()), [])

  useEffect(() => {
    if (reduced) return
    const ctx = gsap.context(() => {
      const states = gsap.utils.toArray('.sig-state')
      const bars = gsap.utils.toArray('.sig-bar')
      const stats = gsap.utils.toArray('.sig-stat')

      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: 'top top',
          end: '+=220%',
          scrub: 0.6,
          pin: true,
        },
      })
      tl.set(states, { opacity: 0 })
        .set(states[0], { opacity: 1 })
        .to(states[0], { opacity: 0, duration: 0.4 }, 0.12)
        .to(states[1], { opacity: 1, duration: 0.4 }, 0.12)
        .to(states[1], { opacity: 0, duration: 0.4 }, 0.32)
        .to(states[2], { opacity: 1, duration: 0.4 }, 0.32)
        .fromTo(bars, { scaleY: 0 }, { scaleY: 1, duration: 0.9, stagger: 0.012, ease: 'power2.out', transformOrigin: 'bottom' }, 0.4)
        .to('.sig-symbol', { color: '#ff3b4e', duration: 0.3 }, 0.62)
        .fromTo(stats, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.4, stagger: 0.08 }, 0.68)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  return (
    <section className="signal-viz" ref={rootRef} id="signal">
      <div className="wrap sig-inner">
        <div className="sig-states">
          <span className="sig-state">NORMAL</span>
          <span className="sig-state">UNUSUAL</span>
          <span className="sig-state">SIGNAL DETECTED</span>
        </div>

        <div className="sig-head">
          <span className="sig-symbol">NVDA</span>
          <span className="demo-tag">Demo data</span>
        </div>

        <svg className="sig-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
          <line className="sig-grid" x1="0" y1={H * 0.25} x2={W} y2={H * 0.25} />
          <line className="sig-grid" x1="0" y1={H * 0.5} x2={W} y2={H * 0.5} />
          <line className="sig-grid" x1="0" y1={H * 0.75} x2={W} y2={H * 0.75} />
          <line className="sig-trig" x1={data.trigX} y1="0" x2={data.trigX} y2={H} />
          {data.bars.map((b, i) => (
            <g key={i} className={`sig-bar${b.alert ? ' is-alert' : ''}`}>
              <line x1={b.cx} y1={b.wickY1} x2={b.cx} y2={b.wickY2} className="sig-wick" />
              <rect x={b.cx - b.bw / 2} y={b.bodyTop} width={b.bw} height={b.bodyH} className="sig-body" />
            </g>
          ))}
        </svg>

        <div className="sig-stats">
          {STATS.map((s) => (
            <div className="sig-stat" key={s.label}>
              <span className="sig-stat-label">{s.label}</span>
              <span className="sig-stat-val">{s.value}</span>
              <span className="sig-stat-delta">{s.delta}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
