import { Suspense, useEffect, useRef, useState } from 'react'
import { Canvas } from '@react-three/fiber'
import gsap from 'gsap'
import HeroScene from '../scenes/HeroScene'
import MagneticButton from './MagneticButton'
import { useReducedMotion, useIsMobile } from '../hooks/usePrefs'
import './Hero.css'

function useEtClock() {
  const [time, setTime] = useState('')
  useEffect(() => {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    })
    const tick = () => setTime(fmt.format(new Date()))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return time
}

export default function Hero() {
  const reduced = useReducedMotion()
  const isMobile = useIsMobile()
  const time = useEtClock()
  const rootRef = useRef(null)

  useEffect(() => {
    if (!rootRef.current) return
    const ctx = gsap.context(() => {
      const tl = gsap.timeline({ delay: 0.3, defaults: { ease: 'power3.out' } })
      tl.set('.hero-line-i', { yPercent: 115 })
        .set('.hero-fade', { opacity: 0, y: 16 })
        .to('.hero-line-i', { yPercent: 0, duration: 1.1, stagger: 0.09 })
        .to('.hero-fade', { opacity: 1, y: 0, duration: 0.9, stagger: 0.08 }, '-=0.6')
    }, rootRef)
    return () => ctx.revert()
  }, [])

  return (
    <header className="hero" ref={rootRef} id="top">
      <div className="hero-canvas">
        <Canvas
          dpr={[1, isMobile ? 1.3 : 2]}
          camera={{ position: [0, 0, 5], fov: 45 }}
          gl={{ antialias: true, alpha: false }}
        >
          <Suspense fallback={null}>
            <HeroScene reduced={reduced || isMobile} />
          </Suspense>
        </Canvas>
      </div>

      <div className="hero-vignette" aria-hidden="true" />

      <div className="hero-content wrap">
        <div className="hero-fade eyebrow">
          <span className="dot" />
          PERCH <b>/</b> SYSTEM ACTIVE <b>/</b> <span className="hero-clock">{time || '--:--:--'} ET</span>
        </div>

        <h1>
          <span className="hero-line"><span className="hero-line-i">THE MARKET MOVES.</span></span>
          <span className="hero-line"><span className="hero-line-i">PERCH <em>NOTICES.</em></span></span>
        </h1>

        <p className="hero-fade hero-sub">
          It watches continuously. It knows what's normal for six names, all session.
          When something isn't, you hear about it — in plain language, seconds after it happens.
        </p>

        <div className="hero-fade hero-cta">
          <MagneticButton as="a" href="#waitlist">Request early access</MagneticButton>
          <a className="hero-scroll-hint" href="#field" data-cursor="link">
            <span>Enter Perch</span>
            <span className="hero-scroll-line" />
          </a>
        </div>
      </div>
    </header>
  )
}
