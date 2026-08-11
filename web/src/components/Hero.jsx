import { Suspense, useEffect, useRef, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import gsap from 'gsap'
import HeroScene from '../scenes/HeroScene'
import MagneticButton from './MagneticButton'
import { useReducedMotion, useIsMobile } from '../hooks/usePrefs'
import { SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './Hero.css'

const SESSION_LABEL = { pre: 'PRE-MARKET', open: 'MARKET OPEN', post: 'AFTER HOURS', closed: 'MARKET CLOSED' }

// Mirrors web-app/src/hooks/useMarketClock.js's sessionState() exactly --
// same boundaries (9:30-16:00 ET weekdays), same labels -- so the site and
// the app never disagree about what "after hours" means. Deliberately no
// holiday awareness, same as that hook: this is session hours, not a
// live-verified "market is open today" claim.
function sessionState(etDate) {
  const day = etDate.getDay()
  if (day === 0 || day === 6) return 'closed'
  const minutes = etDate.getHours() * 60 + etDate.getMinutes()
  if (minutes < 9 * 60 + 30) return 'pre'
  if (minutes < 16 * 60) return 'open'
  return 'post'
}

function useEtClock() {
  const [state, setState] = useState({ time: '', sessionLabel: '' })
  useEffect(() => {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    })
    const tick = () => {
      const now = new Date()
      const etParts = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }))
      setState({ time: fmt.format(now), sessionLabel: SESSION_LABEL[sessionState(etParts)] })
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return state
}

export default function Hero() {
  const reduced = useReducedMotion()
  const isMobile = useIsMobile()
  const { time, sessionLabel } = useEtClock()
  const rootRef = useRef(null)
  const [inView, setInView] = useState(true)

  // The single biggest performance win here: stop driving the WebGL
  // render loop entirely once the hero scrolls out of view, instead of
  // letting it render forever in the background behind every other section.
  useEffect(() => {
    if (!rootRef.current || !('IntersectionObserver' in window)) return
    const io = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { threshold: 0.01 })
    io.observe(rootRef.current)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    if (!rootRef.current) return
    // Reduced-motion users never see BootSequence's overlay (it returns
    // null immediately), so the headline is already visible in its final
    // state on first paint -- running this timeline anyway would snap it
    // hidden and replay the reveal on top of content the user can already
    // read, the opposite of what "reduced motion" asked for.
    if (reduced) return
    const ctx = gsap.context(() => {
      // Boot sequence holds the stage for ~1.3s before its own fade starts
      // (see BootSequence) -- the reveal begins right as that fade does, so
      // the headline arrives as the boot dissolves rather than after a gap.
      const tl = gsap.timeline({ delay: 1.2, defaults: { ease: 'power3.out' } })
      tl.set('.hero-line-i', { yPercent: 115 })
        .set('.hero-fade', { opacity: 0, y: 16 })
        .set('h1', { '--wght': 300 })
        .to('.hero-line-i', { yPercent: 0, duration: 1.1, stagger: 0.09 })
        .to('h1', { '--wght': 640, duration: 1.3, ease: 'power2.out' }, '-=1.1')
        .to('.hero-fade', { opacity: 1, y: 0, duration: 0.9, stagger: 0.08 }, '-=0.6')
    }, rootRef)
    return () => ctx.revert()
  }, [reduced])

  return (
    <header className="hero" ref={rootRef} id="top">
      <div className="hero-canvas">
        <Canvas
          frameloop="demand"
          dpr={[1, isMobile ? 1.3 : 1.5]}
          camera={{ position: [0, 0, 5], fov: 45 }}
          gl={{ antialias: true, alpha: false, powerPreference: 'low-power' }}
        >
          <Suspense fallback={null}>
            <HeroScene reduced={reduced} isMobile={isMobile} active={inView} heroRootRef={rootRef} />
          </Suspense>
        </Canvas>
      </div>

      <div className="hero-vignette" aria-hidden="true" />

      <div className="hero-content wrap">
        {/* Pure text content inside the same .hero-fade element the boot
            timeline already animates as a whole (see the GSAP timeline
            below, which targets `.hero-fade` by class, not by count) --
            adding sessionLabel here doesn't add a new animation target or
            touch the timeline at all. */}
        <div className="hero-fade eyebrow">
          <span className="dot" />
          PERCH <b>/</b> SYSTEM ACTIVE {sessionLabel && <><b>/</b> {sessionLabel} </>}<b>/</b>{' '}
          <span className="hero-clock">{time || '--:--:--'} ET</span>
        </div>

        <h1>
          <span className="hero-line"><span className="hero-line-i">THE MARKET MOVES.</span></span>
          <span className="hero-line"><span className="hero-line-i">PERCH <em>NOTICES.</em></span></span>
        </h1>

        <p className="hero-fade hero-sub">
          Perch watches the market continuously and surfaces the things worth your attention —
          in plain language, seconds after they happen.
        </p>

        <div className="hero-fade hero-cta">
          <MagneticButton as="a" href={withRef(SIGNUP_URL)} onClick={() => track('signup_cta_click', { source: 'hero' })}>Sign up</MagneticButton>
          <a className="hero-scroll-hint" href="#field" data-cursor="link">
            <span>See how Perch works</span>
            <span className="hero-scroll-line" />
          </a>
        </div>
      </div>
    </header>
  )
}
