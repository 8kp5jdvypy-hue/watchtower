import { useRef, useState } from 'react'
import gsap from 'gsap'
import MagneticButton from './MagneticButton'
import './Waitlist.css'

const FORM_ENDPOINT = '' // point this at Formspree, Buttondown, or a real backend later

const WATCH_OPTIONS = [
  { key: 'stocks', label: 'Stocks' },
  { key: 'etfs', label: 'ETFs' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'options', label: 'Options' },
  { key: 'everything', label: 'Everything' },
]

export default function Waitlist() {
  const [status, setStatus] = useState('idle') // idle | sending | done | error
  const [email, setEmail] = useState('')
  const [watch, setWatch] = useState([])
  const sweepRef = useRef(null)
  const stageRef = useRef(null)

  function toggleWatch(key) {
    if (key === 'everything') {
      setWatch((w) => (w.includes('everything') ? [] : ['everything']))
      return
    }
    setWatch((w) => {
      const next = w.filter((k) => k !== 'everything')
      return next.includes(key) ? next.filter((k) => k !== key) : [...next, key]
    })
  }

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
        body: JSON.stringify({ email, watch }),
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
        <span className="eyebrow"><span className="dot" /> PERCH / PRIVATE ACCESS</span>
        <h2>Request access.</h2>
        <p className="wl-sub">We're opening Perch gradually to early users.</p>

        {status !== 'done' ? (
          <form className="wl-form" onSubmit={onSubmit}>
            <div className="wl-field">
              <label htmlFor="wl-email" className="sr-only">Your email</label>
              <input
                id="wl-email"
                type="email"
                required
                placeholder="EMAIL"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>

            <fieldset className="wl-watch">
              <legend>What do you want Perch to watch?</legend>
              <div className="wl-chips">
                {WATCH_OPTIONS.map((o) => (
                  <button
                    type="button"
                    key={o.key}
                    className={`wl-chip${watch.includes(o.key) ? ' is-on' : ''}`}
                    onClick={() => toggleWatch(o.key)}
                    aria-pressed={watch.includes(o.key)}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </fieldset>

            <MagneticButton type="submit" disabled={status === 'sending'}>
              {status === 'sending' ? 'Requesting…' : 'Request access'}
            </MagneticButton>
          </form>
        ) : (
          <div className="wl-done">
            <span className="wl-done-mark">✓</span>
            <div>
              <p className="wl-done-title">REQUEST RECEIVED. WELCOME TO PERCH.</p>
              <p className="wl-done-sub">We'll let you know when your access is ready.</p>
            </div>
          </div>
        )}
        {status === 'error' && <p className="wl-error">Signup isn't wired up yet — check back soon.</p>}

        <p className="wl-fine">No spam. No fabricated waitlist numbers. Unsubscribe anytime.</p>
      </div>
      <div className="wl-sweep" ref={sweepRef} aria-hidden="true" />
    </section>
  )
}
