import { useRef, useState } from 'react'
import gsap from 'gsap'
import MagneticButton from './MagneticButton'
import './Waitlist.css'

const FORM_ENDPOINT = '' // point this at Formspree, Buttondown, etc.

export default function Waitlist() {
  const [status, setStatus] = useState('idle') // idle | sending | done | error
  const [email, setEmail] = useState('')
  const sweepRef = useRef(null)
  const stageRef = useRef(null)

  async function onSubmit(e) {
    e.preventDefault()
    if (!email || !e.target.checkValidity()) return
    if (!FORM_ENDPOINT) {
      setStatus('error')
      return
    }
    setStatus('sending')
    const tl = gsap.timeline()
    tl.to(stageRef.current, { '--dark': 1, duration: 0.5, ease: 'power2.in' })
      .fromTo(sweepRef.current, { xPercent: -120, opacity: 1 }, { xPercent: 120, duration: 0.9, ease: 'power2.inOut' })

    try {
      const res = await fetch(FORM_ENDPOINT, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      })
      if (!res.ok) throw new Error('bad status')
      setStatus('done')
    } catch {
      setStatus('error')
    }
  }

  return (
    <section className="waitlist" id="waitlist">
      <div className="wrap wl-inner" ref={stageRef}>
        <span className="eyebrow"><span className="dot" /> STAGE ONE</span>
        <h2>Enter the first wave.</h2>
        <p className="wl-sub">Perch is opening in stages. Be among the first to experience it.</p>

        {status !== 'done' ? (
          <form className="wl-form" onSubmit={onSubmit}>
            <div className="wl-field">
              <label htmlFor="wl-email" className="sr-only">Your email</label>
              <input
                id="wl-email"
                type="email"
                required
                placeholder="YOUR EMAIL"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>
            <MagneticButton type="submit" disabled={status === 'sending'}>
              {status === 'sending' ? 'Requesting…' : 'Join the waitlist'}
            </MagneticButton>
          </form>
        ) : (
          <div className="wl-done">
            <span className="wl-done-mark">✓</span>
            <div>
              <p className="wl-done-title">ACCESS REQUESTED. YOU'RE IN.</p>
              <p className="wl-done-sub">We'll let you know when Perch opens.</p>
            </div>
          </div>
        )}
        {status === 'error' && <p className="wl-error">Signup isn't wired up yet — check back soon.</p>}

        <p className="wl-fine">No spam. No fake scarcity. Unsubscribe anytime.</p>
      </div>
      <div className="wl-sweep" ref={sweepRef} aria-hidden="true" />
    </section>
  )
}
