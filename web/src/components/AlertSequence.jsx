import { useEffect, useMemo, useRef } from 'react'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { buildCandles } from '../scenes/candleData'
import { useReducedMotion, useIsMobile } from '../hooks/usePrefs'
import './AlertSequence.css'

// A small, quiet field of names -- not a trading terminal. Same
// deterministic-seed technique as MarketField, but its own independent
// layout: this section tells its own version of the story at closer range.
const SYMBOLS = ['AMD', 'AAPL', 'TSLA', 'MSFT', 'SPY', 'QQQ', 'SOXX', 'XLK', 'NVDA']
const CHOSEN = 'NVDA'

function useField(count) {
  return useMemo(() => {
    const rnd = (seed => () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed / 0x7fffffff
    })(29)
    const items = []
    for (let i = 0; i < count; i++) {
      const sym = SYMBOLS[i % SYMBOLS.length]
      const isChosen = sym === CHOSEN && !items.some((it) => it.isChosen)
      items.push({
        sym,
        // The chosen symbol starts scattered in the field like everything
        // else, deliberately -- beat 5 moves it to center. If it started
        // already centered there'd be no "one thing separates from the
        // noise" to see.
        left: isChosen ? 42 + rnd() * 16 : 8 + rnd() * 84,
        top: isChosen ? 40 + rnd() * 16 : 10 + rnd() * 80,
        size: isChosen ? 1.1 : 0.8 + rnd() * 0.55,
        delay: rnd() * 3,
        isChosen,
      })
    }
    return items
  }, [count])
}

const CONTEXT_ROWS = [
  { sym: 'NVDA', name: 'The name', chg: '+4.2%' },
  { sym: 'SOXX', name: 'Its sector — semiconductors', chg: '+1.2%' },
  { sym: 'XLK', name: 'Broader technology', chg: '+0.8%' },
  { sym: 'SPY', name: 'The overall market', chg: '+0.3%' },
]

const STATS = [
  { label: 'PRICE', value: '$146.82', delta: '+6.4%' },
  { label: 'VOLUME', value: '3.1×', delta: 'avg' },
  { label: 'VOLATILITY', value: '2.3 ATR', delta: 'expansion' },
  { label: 'TIME', value: '12:14 PM', delta: 'ET' },
]

const W = 640, H = 220, PAD = 0.14

function toPath({ ohlc, trig, n }) {
  const vals = ohlc.flatMap((d) => [d.hi, d.lo])
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const span = (hi - lo) || 1
  const py = (v) => (1 - (PAD + ((v - lo) / span) * (1 - 2 * PAD))) * H
  const step = W / n
  const bw = step * 0.56
  const bars = ohlc.map((d) => {
    const cx = d.i * step + step / 2
    const bodyTop = py(Math.max(d.o, d.c))
    const bodyH = Math.max(1.4, Math.abs(py(d.o) - py(d.c)))
    return { cx, bw, wickY1: py(d.hi), wickY2: py(d.lo), bodyTop, bodyH, alert: d.i >= trig }
  })
  return { bars, trigX: trig * step + step / 2 }
}

export default function AlertSequence() {
  const reduced = useReducedMotion()
  const isMobile = useIsMobile()
  const count = isMobile ? 22 : 46
  const items = useField(count)
  const chosenItem = useMemo(() => items.find((it) => it.isChosen), [items])
  const chart = useMemo(() => toPath(buildCandles(11, 26, 0.6)), [])
  const rootRef = useRef(null)
  const fieldRef = useRef(null)

  useEffect(() => {
    if (reduced) return
    const ctx = gsap.context(() => {
      const field = fieldRef.current
      const chosen = field.querySelector('.as-sym.is-chosen')
      const others = field.querySelectorAll('.as-sym:not(.is-chosen)')
      const statusPill = rootRef.current.querySelector('.as-status')
      const ring1 = rootRef.current.querySelector('.as-ring-1')
      const ring2 = rootRef.current.querySelector('.as-ring-2')
      const ctxRows = gsap.utils.toArray('.as-ctx-row')
      const context = rootRef.current.querySelector('.as-context')
      const point = rootRef.current.querySelector('.as-point')
      const line = rootRef.current.querySelector('.as-point-line')
      const signalLabel = rootRef.current.querySelector('.as-signal-label')
      const bars = gsap.utils.toArray('.as-bar')
      const stats = gsap.utils.toArray('.as-stat')
      const payoff = rootRef.current.querySelector('.as-payoff')

      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: 'top top',
          // 340% made this the longest pin on the site by a wide margin
          // (MarketField is 140%, the old SignalVisualization was 220%)
          // -- it read as dragging relative to everything around it.
          // Tightened to sit closer to that established pace; the beat
          // choreography below is all relative fractions, so this just
          // compresses the whole thing to be quicker per pixel scrolled.
          end: '+=200%',
          scrub: 0.6,
          pin: true,
        },
      })

      // Beat 1 -- the field appears, quiet, unremarkable. The payoff layer
      // (chart grid, stats grid) carries its own always-opaque background/
      // border chrome, so it has to be fully hidden up front too, not just
      // its individual text values -- otherwise that chrome sits visible,
      // overlapping the field, for the whole first half of the sequence.
      tl.fromTo(field, { opacity: 0 }, { opacity: 1, duration: 0.06 }, 0)
        .set(payoff, { opacity: 0 }, 0)

      // Beat 2 -- one name starts to separate from the noise. The status
      // reads SCANNING (neutral) until it doesn't.
      tl.set(statusPill, { innerText: 'SCANNING' }, 0.05)
        .to(chosen, { color: 'var(--amber)', scale: 1.15, duration: 0.14 }, 0.14)
        .set(statusPill, { innerText: 'UNUSUAL ACTIVITY' }, 0.16)
        .to(statusPill, { color: 'var(--amber)', duration: 0.1 }, 0.16)

      // Beat 3 -- Perch scans. Two soft rings ignite from the name itself
      // and expand outward, fading -- not a literal radar, just a pulse of
      // attention landing exactly where the anomaly is.
      tl.fromTo([ring1, ring2], { scale: 0.2, opacity: 0.5 },
        { scale: 5.5, opacity: 0, duration: 0.26, stagger: 0.08, ease: 'power1.out' }, 0.24)
        .to(chosen, { scale: 1.3, duration: 0.1 }, 0.26)

      // Beat 4 -- context, checked one line at a time: the name, then its
      // sector, then the broader market. Perch isn't asking "is NVDA up,"
      // it's asking "is NVDA up relative to what's around it." The field
      // dims gradually across this same span, rather than staying at full
      // brightness underneath the table and only fading afterward --
      // otherwise the two layers visibly collide.
      tl.to([others, statusPill], { opacity: 0.06, duration: 0.34, ease: 'none' }, 0.42)
        .to(others, { scale: 0.8, stagger: { each: 0.002, from: 'random' }, duration: 0.3 }, 0.42)
      ctxRows.forEach((row, i) => {
        tl.to(row, { opacity: 1, x: 0, duration: 0.1, ease: 'power2.out' }, 0.42 + i * 0.06)
      })

      // Beat 5 -- context is checked; the table clears, the name itself
      // moves to center and resolves to cyan, becoming the payoff's
      // focal point. The context layer and the payoff layer occupy the
      // same space, so one has to be gone before the other arrives.
      tl.to(context, { opacity: 0, duration: 0.1 }, 0.7)
        .to(chosen, { left: '50%', top: '38%', scale: 1.6, color: 'var(--cyan)', duration: 0.22, ease: 'power2.inOut' }, 0.7)
        .to(field, { opacity: 0, duration: 0.1 }, 0.86)
        .set(payoff, { opacity: 1 }, 0.78)

      // Beat 6 -- the payoff. A point, a line, a confirmed signal -- then
      // the chart draws in underneath it as the receipt.
      tl.fromTo(point, { scale: 0, opacity: 0 }, { scale: 1, opacity: 1, duration: 0.05 }, 0.8)
        .fromTo(line, { scaleX: 0 }, { scaleX: 1, duration: 0.06, transformOrigin: 'left center' }, 0.83)
        .fromTo(signalLabel, { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.08 }, 0.87)
        .fromTo(bars, { scaleY: 0 }, { scaleY: 1, duration: 0.1, stagger: 0.006, ease: 'power2.out', transformOrigin: 'bottom' }, 0.88)
        .fromTo(stats, { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.07, stagger: 0.03 }, 0.93)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced, isMobile])

  return (
    <section className="alert-seq" ref={rootRef} id="signal">
      <div className="wrap as-inner">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> HOW A SIGNAL FORMS</span>
          <h2>A move only means something in context.</h2>
          <p className="as-sub">
            Perch checks a move against its sector, the broader market, and what's normal for
            that name — before deciding it's worth your attention. <span className="demo-tag">Demo</span>
          </p>
        </div>

        <div className="as-stage">
          <div className="as-field" ref={fieldRef}>
            <span
              className="as-ring as-ring-1"
              style={{ left: `${chosenItem.left}%`, top: `${chosenItem.top}%` }}
              aria-hidden="true"
            />
            <span
              className="as-ring as-ring-2"
              style={{ left: `${chosenItem.left}%`, top: `${chosenItem.top}%` }}
              aria-hidden="true"
            />
            {items.map((it, i) => (
              <span
                key={i}
                className={`as-sym${it.isChosen ? ' is-chosen' : ''}`}
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
            <span className="as-status" aria-hidden="true">SCANNING</span>
          </div>

          <div className="as-context" aria-hidden="true">
            {CONTEXT_ROWS.map((r) => (
              <div className="as-ctx-row" key={r.sym}>
                <span className="as-ctx-sym">{r.sym}</span>
                <span className="as-ctx-name">{r.name}</span>
                <span className="as-ctx-chg">{r.chg}</span>
              </div>
            ))}
          </div>

          <div className="as-payoff" aria-hidden="true">
            <span className="as-signal-flourish">
              <span className="as-point" />
              <span className="as-point-line" />
            </span>
            <span className="as-signal-label"><span className="dot" /> SIGNAL DETECTED</span>
            <svg className="as-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
              <line className="as-grid" x1="0" y1={H * 0.3} x2={W} y2={H * 0.3} />
              <line className="as-grid" x1="0" y1={H * 0.65} x2={W} y2={H * 0.65} />
              <line className="as-trig" x1={chart.trigX} y1="0" x2={chart.trigX} y2={H} />
              {chart.bars.map((b, i) => (
                <g key={i} className={`as-bar${b.alert ? ' is-alert' : ''}`}>
                  <line x1={b.cx} y1={b.wickY1} x2={b.cx} y2={b.wickY2} className="as-wick" />
                  <rect x={b.cx - b.bw / 2} y={b.bodyTop} width={b.bw} height={b.bodyH} className="as-body" />
                </g>
              ))}
            </svg>
            <div className="as-stats">
              {STATS.map((s) => (
                <div className="as-stat" key={s.label}>
                  <span className="as-stat-label">{s.label}</span>
                  <span className="as-stat-val">{s.value}</span>
                  <span className="as-stat-delta">{s.delta}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
