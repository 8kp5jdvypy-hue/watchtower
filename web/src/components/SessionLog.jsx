import { useEffect, useRef, useState } from 'react'
import { etParts, latestSession, outcomeOf } from '../lib/perchData'
import { useReducedMotion } from '../hooks/usePrefs'
import './SessionLog.css'

/*
 * The signature moment: the latest session's real alerts, in order,
 * each lighting as it enters view (IntersectionObserver — no library).
 * Every row is a real journal row with its real 30-minute outcome;
 * reversals and pending outcomes are shown exactly as the record has
 * them. The mobile edition carries its own compact rail because the
 * fixed one is hidden under 960px.
 */
export default function SessionLog({ record }) {
  const reduced = useReducedMotion()
  const { dateKey, alerts, pending } = latestSession(record?.alerts)
  const [lit, setLit] = useState(() => new Set())
  const listRef = useRef(null)
  useEffect(() => {
    if (reduced || !listRef.current) return undefined
    const rows = [...listRef.current.querySelectorAll('[data-row]')]
    const io = new IntersectionObserver((entries) => {
      setLit((prev) => {
        const next = new Set(prev)
        for (const e of entries) if (e.isIntersecting) next.add(e.target.dataset.row)
        return next
      })
    }, { rootMargin: '0px 0px -30% 0px', threshold: 0.2 })
    rows.forEach((r) => io.observe(r))
    return () => io.disconnect()
  }, [reduced, alerts.length])

  const hits = alerts.filter((a) => outcomeOf(a).kind === 'continued').length
  const scored = alerts.filter((a) => a.return_pct != null).length
  const day = dateKey ? etParts(alerts[0].sent_at).date : null

  return (
    <section className="log" id="session" data-session-minutes="600" data-session-minutes-end="960">
      <p className="eyebrow"><b>09:30 → 16:00</b> the session</p>
      <h2 className="h2">{day ? `${day}, as it happened.` : 'A session, as it happens.'}</h2>
      <p className="lede">
        {alerts.length
          ? <>Every alert Perch sent that day, at the minute it sent it, with the outcome the journal recorded thirty minutes later. {scored ? <>{hits} of {scored} continued.</> : null} Nothing curated{pending ? ' — the most recent session whose outcomes are already marked; today\u2019s are still pending' : ''}.</>
          : 'The public record is unreachable right now. When it is back, this is the day’s real log — not a demo.'}
      </p>
      <ol className="log-list" ref={listRef}>
        {alerts.map((a, i) => {
          const t = etParts(a.sent_at)
          const o = outcomeOf(a)
          const on = reduced || lit.has(a.detection_id)
          return (
            <li key={a.detection_id} data-row={a.detection_id} className={`log-row ${on ? 'is-lit' : ''} row-${o.kind}`} style={{ '--i': i }}>
              <span className="log-time">{t.time}</span>
              <span className="log-rail" aria-hidden="true"><span className="log-dot" /></span>
              <span className="log-body">
                <span className="log-head"><b>{a.symbol}</b> <span className="log-origin">{a.origin === 'watchlist' ? 'watchlist' : 'radar'}</span> <span className={`log-trend trend-${a.trend}`}>{a.trend}</span></span>
                <span className="log-headline">{a.headline}</span>
                <span className="log-outcome">{o.label}</span>
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
