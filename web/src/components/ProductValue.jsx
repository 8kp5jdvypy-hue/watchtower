import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { useReducedMotion } from '../hooks/usePrefs'
import './ProductValue.css'

// Everything a person would have to track themselves, if they wanted to
// spot the same things Perch does. Not exhaustive by design -- it trails
// off into "..." on purpose, because the actual point is that the list
// doesn't have a comfortable end.
const WATCH_ITEMS = [
  'Hundreds of tickers', 'Price movements', 'Volume', 'Sector movement',
  'Market context', 'News', 'Technical activity',
]

export default function ProductValue() {
  const reduced = useReducedMotion()
  const rootRef = useRef(null)

  useEffect(() => {
    if (reduced) return
    const ctx = gsap.context(() => {
      gsap.timeline({
        scrollTrigger: { trigger: rootRef.current, start: 'top 75%' },
      })
        .from('.value-watch-list li', { opacity: 0, y: 8, duration: 0.4, stagger: 0.06 })
        .from('.value-or', { opacity: 0, duration: 0.35 }, '+=0.1')
        .from('.value-headline', { opacity: 0, y: 14, duration: 0.55, ease: 'power2.out' }, '-=0.1')
        .from('.value-lines p', { opacity: 0, y: 10, duration: 0.4, stagger: 0.1 }, '-=0.2')
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  return (
    <section className="value" id="value" ref={rootRef}>
      <div className="wrap value-inner">
        <span className="eyebrow"><span className="dot" /> PERCH / THE DIFFERENCE</span>

        <p className="value-watch-label">YOU COULD WATCH:</p>
        <ul className="value-watch-list">
          {WATCH_ITEMS.map((item) => <li key={item}>{item}</li>)}
          <li className="value-watch-etc" aria-hidden="true">and it never really stops.</li>
        </ul>

        <p className="value-or">OR</p>

        <h2 className="value-headline">LET PERCH WATCH.</h2>

        <div className="value-lines">
          <p>Perch filters the noise.</p>
          <p>Perch surfaces what deserves attention.</p>
          <p>Perch explains why.</p>
        </div>
      </div>
    </section>
  )
}
