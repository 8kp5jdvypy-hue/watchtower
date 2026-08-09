import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import MagneticButton from './MagneticButton'
import SignalGlyph from './SignalGlyph'
import './Waitlist.css'

// FormSubmit.co -- no account signup required. The first submission ever
// sent here triggers a one-time confirmation email to the destination
// address; click the link in it once and every submission after that
// delivers automatically. Swap the address if you want a different inbox.
const FORM_ENDPOINT = 'https://formsubmit.co/ajax/anthony.creds@gmail.com'

const WATCH_OPTIONS = [
  { key: 'stocks', label: 'Stocks' },
  { key: 'etfs', label: 'ETFs' },
  { key: 'crypto', label: 'Crypto' },
  { key: 'options', label: 'Options' },
  { key: 'everything', label: 'Everything' },
]

export default function Waitlist() {
  const [status, setStatus] = useState('idle') // idle | sending | done | error
  const [welcomed, setWelcomed] = useState(false)
  const [email, setEmail] = useState('')
  const [watch, setWatch] = useState([])
  const sweepRef = useRef(null)
  const stageRef = useRef(null)

  // The market visualization quiets, a signal pulse moves through, and only
  // then does the final "welcome" copy arrive -- a small ceremony for what
  // is, for the visitor, a real moment: they just asked to get in.
  useEffect(() => {
    if (status !== 'done') return
    const id = setTimeout(() => setWelcomed(true), 1100)
    return () => clearTimeout(id)
  }, [status])

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
      setStatus('unconfigured')
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
      // FormSubmit.co answers with HTTP 200 even when it hasn't actually
      // delivered anything yet -- e.g. before its one-time destination-
      // email activation is confirmed, the body carries success:"false"
      // with the real status. Trusting res.ok alone would show visitors
      // a confident "welcome to Perch" for a submission that silently
      // went nowhere, so the response body is what actually decides this.
      const data = await res.json().catch(() => null)
      if (!res.ok || data?.success === 'false' || data?.success === false) {
        throw new Error(data?.message || 'submission not accepted')
      }
      setStatus('done')
    } catch {
      setStatus('error')
      // The submit animation darkens the stage and never reverses it on
      // its own -- only the success path was accounted for. Without this,
      // a failed submission (or a retry) leaves the form permanently
      // behind a blacked-out overlay.
      gsap.to(stageRef.current, { '--dark': 0, duration: 0.4, ease: 'power2.out' })
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
          <div className={`wl-done${welcomed ? ' is-welcomed' : ''}`}>
            <SignalGlyph className="wl-done-glyph" />
            <p className="wl-done-title">
              <span className="wl-done-stage1">ACCESS REQUESTED.</span>
              <span className="wl-done-stage2">WELCOME TO PERCH.</span>
            </p>
            <p className="wl-done-sub">We'll let you know when your access is ready.</p>
          </div>
        )}
        {status === 'unconfigured' && <p className="wl-error">Signup isn't wired up yet — check back soon.</p>}
        {status === 'error' && <p className="wl-error">Something went wrong. Please try again in a moment.</p>}

        <p className="wl-fine">No spam. No fabricated waitlist numbers. Unsubscribe anytime.</p>
      </div>
      <div className="wl-sweep" ref={sweepRef} aria-hidden="true" />
    </section>
  )
}
