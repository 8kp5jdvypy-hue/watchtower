import { useEffect, useMemo, useRef, useState } from 'react'
import { etParts, outcomeOf } from '../data/perchData'
import { useReducedMotion } from '../hooks/usePrefs'
import './SessionRail.css'

/*
 * The session rail — the site's signature (DESIGN-DIRECTION-2026-09.md).
 *
 * A vertical time scale for one trading day, 04:00 → 20:00 ET, fixed to
 * the left edge on wide screens. The page's sections each declare the
 * session time they belong to (data-session-minutes); as you scroll,
 * the marker advances through the day, and the real alerts from the
 * latest session in the public record are pinned at the minute they
 * were sent. When the marker passes one, it lights.
 *
 * Reduced motion: the marker still tracks scroll (that is position,
 * not animation) but nothing eases, and every pin is lit from the start
 * so the day is legible at rest.
 */

const DAY_START = 4 * 60
const DAY_END = 20 * 60
const TICKS = [4, 6, 8, 9.5, 10, 12, 14, 16, 18, 20]

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)) }
function pct(minutes) { return ((minutes - DAY_START) / (DAY_END - DAY_START)) * 100 }

/** Reads every [data-session-minutes] section and interpolates the
 * current session minute from where the viewport's focus line sits. */
function useSessionMinute() {
  const [minute, setMinute] = useState(DAY_START + 180)
  useEffect(() => {
    const sections = [...document.querySelectorAll('[data-session-minutes]')]
    if (!sections.length) return undefined
    let raf = 0
    const compute = () => {
      raf = 0
      const focus = window.innerHeight * 0.42
      const points = sections.map((el) => {
        const r = el.getBoundingClientRect()
        return { y: r.top, h: r.height, m: Number(el.dataset.sessionMinutes), end: Number(el.dataset.sessionMinutesEnd || el.dataset.sessionMinutes) }
      })
      let value = points[0].m
      for (let i = 0; i < points.length; i++) {
        const p = points[i]
        const next = points[i + 1]
        if (focus < p.y) break
        if (focus >= p.y && focus < p.y + p.h) {
          const t = clamp((focus - p.y) / Math.max(1, p.h), 0, 1)
          value = p.m + (p.end - p.m) * t
          break
        }
        if (next && focus >= p.y + p.h && focus < next.y) {
          const t = clamp((focus - (p.y + p.h)) / Math.max(1, next.y - (p.y + p.h)), 0, 1)
          value = p.end + (next.m - p.end) * t
          break
        }
        value = p.end
      }
      setMinute(clamp(value, DAY_START, DAY_END))
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(compute) }
    compute()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => { window.removeEventListener('scroll', onScroll); window.removeEventListener('resize', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [])
  return minute
}

export default function SessionRail({ alerts = [], sessionLabel }) {
  const reduced = useReducedMotion()
  const minute = useSessionMinute()
  const railRef = useRef(null)
  const pins = useMemo(() => alerts.map((a) => {
    const t = etParts(a.sent_at)
    return { id: a.detection_id, m: t.minutesOfDay, time: t.time, symbol: a.symbol, headline: a.headline, outcome: outcomeOf(a) }
  }).filter((p) => p.m >= DAY_START && p.m <= DAY_END), [alerts])

  const hh = String(Math.floor(minute / 60)).padStart(2, '0')
  const mm = String(Math.floor(minute % 60)).padStart(2, '0')

  return (
    <aside className={`rail ${reduced ? 'rail-static' : ''}`} ref={railRef} aria-label="Trading session timeline">
      <div className="rail-head">
        <span className="rail-label">{(sessionLabel || 'Session').replace(/^\w{3} /, '')}</span>
        <span className="rail-tz">ET</span>
      </div>
      <div className="rail-scale">
        <div className="rail-line" />
        {TICKS.map((h) => (
          <div key={h} className={`rail-tick ${h === 9.5 || h === 16 ? 'rail-tick-major' : ''}`} style={{ top: `${pct(h * 60)}%` }}>
            <span>{h === 9.5 ? '09:30' : `${String(h).padStart(2, '0')}:00`}</span>
          </div>
        ))}
        <div className="rail-open" style={{ top: `${pct(9.5 * 60)}%`, height: `${pct(16 * 60) - pct(9.5 * 60)}%` }} aria-hidden="true" />
        {pins.map((p) => (
          <div
            key={p.id}
            className={`rail-pin ${minute >= p.m || reduced ? 'is-lit' : ''} pin-${p.outcome.kind}`}
            style={{ top: `${pct(p.m)}%` }}
            data-rail-pin={p.id}
            tabIndex={0}
            aria-label={`${p.time} ET, ${p.symbol}: ${p.headline}. ${p.outcome.label}`}
          >
            <span className="rail-pin-dot" />
            <span className="rail-pin-tip" aria-hidden="true">
              <b>{p.symbol}</b> {p.time} · {p.headline}
              <i>{p.outcome.label}</i>
            </span>
          </div>
        ))}
        <div className="rail-marker" style={{ top: `${pct(minute)}%` }}>
          <span className="rail-now">{hh}:{mm}</span>
        </div>
      </div>
    </aside>
  )
}
