import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { useReducedMotion } from '../hooks/usePrefs'
import { PerchMarkGlyph, PERCH_MARK_VIEWBOX } from './PerchMark'
import './BootSequence.css'

// "The Full Stop" -- 24 points of noise (the market) pulled into a single
// point, ignited as one cyan signal, revealed to be the period at the end
// of PERCH. The dot never fades; it flies home into the nav's own live
// indicator as the overlay dissolves, so there's no "loading screen" and
// then "site" -- one continuous object, carried straight into the page.
//
// Positions are a deterministic golden-angle scatter, not Math.random() --
// this is a choreographed moment and should look identical every time.
const MOTE_COUNT = 24
const GOLDEN_ANGLE = 137.50776405003785 * (Math.PI / 180)
const MOTES = Array.from({ length: MOTE_COUNT }, (_, i) => {
  const angle = i * GOLDEN_ANGLE
  const r = Math.sqrt((i + 1) / MOTE_COUNT)
  return {
    xPct: 50 + Math.cos(angle) * r * 27,
    yPct: 50 + Math.sin(angle) * r * 15,
    size: 1.6 + (i % 5) * 0.24,
    targetOpacity: 0.12 + (i % 7) * 0.024,
    jitter: ((i * 37) % 120) / 1000, // seconds -- spreads pop-in/pull starts
  }
})

export default function BootSequence() {
  const reduced = useReducedMotion()
  const [done, setDone] = useState(reduced)
  const rootRef = useRef(null)
  const dotRef = useRef(null)

  useEffect(() => {
    if (reduced || !rootRef.current) return
    const ctx = gsap.context(() => {
      const motes = gsap.utils.toArray('.boot-mote')
      const dotRect = dotRef.current.getBoundingClientRect()
      const cx = dotRect.left + dotRect.width / 2
      const cy = dotRect.top + dotRect.height / 2

      // Every mote's straight-line path to the lock point, measured once
      // against real layout -- this is what lets the "streak" be a plain
      // scaleX pulse along a static rotation instead of a curved path.
      const deltas = motes.map((el) => {
        const r = el.getBoundingClientRect()
        const dx = cx - (r.left + r.width / 2)
        const dy = cy - (r.top + r.height / 2)
        return { dx, dy, angle: Math.atan2(dy, dx) * (180 / Math.PI) }
      })
      gsap.set(motes, { rotation: (i) => deltas[i].angle })

      const tl = gsap.timeline({ onComplete: () => setDone(true) })

      // Beat 1 (0 - 0.15s) -- static: the noise appears. Nothing moves yet.
      motes.forEach((el, i) => {
        tl.to(el, { opacity: MOTES[i].targetOpacity, duration: 0.09, ease: 'none' }, MOTES[i].jitter * 0.5)
      })

      // Beat 2 (0.15 - 0.65s) -- the pull: dead-straight acceleration into
      // the lock point, a streak mid-flight, consumed just before arrival.
      motes.forEach((el, i) => {
        const start = 0.15 + MOTES[i].jitter * 0.4
        tl.to(el, { x: deltas[i].dx, y: deltas[i].dy, duration: 0.48, ease: 'power3.in' }, start)
          .to(el, { scaleX: 4.2, duration: 0.18, ease: 'power1.in' }, start + 0.14)
          .to(el, { scaleX: 1, opacity: 0, duration: 0.18, ease: 'power1.out' }, start + 0.32)
      })

      // Beat 3 (0.62 - 0.95s) -- the lock: one cyan dot snaps in with a
      // small overshoot, one ping ring. The only color in the sequence.
      tl.to('.boot-dot', { scale: 1, opacity: 1, duration: 0.18, ease: 'back.out(2)' }, 0.62)
        .fromTo('.boot-ring', { scale: 0.3, opacity: 0.9 }, { scale: 7, opacity: 0, duration: 0.3, ease: 'expo.out' }, 0.64)

      // Beat 4 (0.72 - 1.05s) -- PERCH resolves around the fixed dot. Scale
      // (not letter-spacing) does the "compressing into focus" work, so the
      // dot's position next to it never has to move.
      tl.to('.boot-word', { opacity: 1, scaleX: 1, filter: 'blur(0px)', duration: 0.33, ease: 'expo.out' }, 0.72)

      // Beat 5 (1.05 - 1.43s) -- hold, then the handoff: the word leaves,
      // the dot flies into the nav's live dot, the ground never changes.
      tl.to('.boot-word', { opacity: 0, duration: 0.15, ease: 'power2.in' }, 1.15)
        .add(() => {
          const from = dotRef.current.getBoundingClientRect()
          const target = document.querySelector('.nav-live-dot')
          if (target) {
            const to = target.getBoundingClientRect()
            const dx = (to.left + to.width / 2) - (from.left + from.width / 2)
            const dy = (to.top + to.height / 2) - (from.top + from.height / 2)
            gsap.to('.boot-dot', { x: dx, y: dy, scale: to.width / from.width, duration: 0.28, ease: 'power3.inOut' })
          } else {
            gsap.to('.boot-dot', { scale: 1.4, opacity: 0, duration: 0.12, ease: 'power1.in' })
          }
        }, 1.15)
        .to(rootRef.current, { autoAlpha: 0, duration: 0.3, ease: 'power2.inOut' }, 1.18)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  if (done) return null

  return (
    <div className="boot" ref={rootRef} aria-hidden="true">
      {MOTES.map((m, i) => (
        <span
          key={i}
          className="boot-mote"
          style={{ left: `${m.xPct}%`, top: `${m.yPct}%`, width: `${m.size}px`, height: `${m.size}px` }}
        />
      ))}
      <div className="boot-lock">
        <span className="boot-word">PERCH</span>
        <span className="boot-dot-wrap">
          <span className="boot-ring" />
          {/* The falcon mark itself locks in here -- the exact same glyph
              as the nav/footer icon, via PerchMarkGlyph -- rather than a
              generic dot, so "the signal" and "the brand mark" are
              visibly the same thing. Own <svg> wrapper (not the full
              PerchMark component) because this element itself is the
              GSAP animation target. */}
          <svg className="boot-dot" ref={dotRef} viewBox={PERCH_MARK_VIEWBOX} aria-hidden="true">
            <PerchMarkGlyph fill="currentColor" accent={false} />
          </svg>
        </span>
      </div>
    </div>
  )
}
