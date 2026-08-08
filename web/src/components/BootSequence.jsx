import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { useReducedMotion } from '../hooks/usePrefs'
import './BootSequence.css'

const LINES = [
  'PERCH',
  'INITIALIZING MARKET TELEMETRY',
  'CALIBRATING SIGNAL THRESHOLDS',
  'WATCHING',
]

export default function BootSequence() {
  const reduced = useReducedMotion()
  const [done, setDone] = useState(reduced)
  const rootRef = useRef(null)
  const pctRef = useRef(null)

  useEffect(() => {
    if (reduced || !rootRef.current) return
    const ctx = gsap.context(() => {
      const lines = gsap.utils.toArray('.boot-line')
      const tl = gsap.timeline({ onComplete: () => setDone(true) })
      tl.set(lines, { opacity: 0 })
      lines.forEach((line, i) => {
        tl.to(line, { opacity: 1, duration: 0.18 }, i * 0.28)
          .to(line, { opacity: i === lines.length - 1 ? 1 : 0.25, duration: 0.2 }, i * 0.28 + 0.32)
      })
      tl.to(pctRef.current, { innerText: 100, duration: 1.15, snap: 'innerText', ease: 'power1.inOut' }, 0)
      tl.to('.boot-bar-fill', { width: '100%', duration: 1.15, ease: 'power1.inOut' }, 0)
      tl.to(rootRef.current, { autoAlpha: 0, duration: 0.5, ease: 'power2.inOut' }, 1.35)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  if (done) return null

  return (
    <div className="boot" ref={rootRef} aria-hidden="true">
      <div className="boot-inner">
        <div className="boot-lines">
          {LINES.map((l, i) => (
            <span className="boot-line" key={i}>{l}</span>
          ))}
        </div>
        <div className="boot-bar">
          <span className="boot-bar-fill" />
        </div>
        <div className="boot-pct" ref={pctRef}>0</div>
      </div>
    </div>
  )
}
