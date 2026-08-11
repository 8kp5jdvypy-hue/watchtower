import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { useReducedMotion } from '../hooks/usePrefs'
import './Manifesto.css'

export default function Manifesto() {
  const reduced = useReducedMotion()
  const rootRef = useRef(null)

  useEffect(() => {
    if (reduced) return
    const ctx = gsap.context(() => {
      gsap.timeline({
        scrollTrigger: {
          trigger: rootRef.current,
          start: 'top top',
          end: '+=100%',
          scrub: 0.6,
          pin: true,
        },
      })
        .to('.man-watch', { opacity: 0.15, scale: 0.9, duration: 0.5 }, 0.25)
        .fromTo('.man-decide', { opacity: 0, y: 24 }, { opacity: 1, y: 0, duration: 0.5 }, 0.35)
        .fromTo('.man-lines p', { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.4, stagger: 0.1 }, 0.6)
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  return (
    <section className="manifesto" ref={rootRef}>
      <div className="wrap man-inner">
        <h2>
          <span className="man-watch">WE WATCH.</span>
          <span className="man-decide">YOU DECIDE.</span>
        </h2>
        <div className="man-lines">
          <p>Perch doesn't place trades.</p>
          <p>It doesn't control your account.</p>
          <p>It gives you the information. You make the decision.</p>
        </div>
      </div>
    </section>
  )
}
