import { useEffect, useRef, useState } from 'react'
import PerchMark from './PerchMark'
import { SIGNUP_URL, LOGIN_URL } from '../config'
import './Nav.css'
import './PerchMark.css'

const LINKS = [
  { href: '#field', label: 'What it watches' },
  { href: '#demo', label: 'Interface' },
  { href: '#coverage', label: 'Markets' },
  { href: '#value', label: 'Why Perch' },
]

export default function Nav() {
  const [scrolled, setScrolled] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const barRef = useRef(null)

  useEffect(() => {
    let raf
    const onScroll = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = null
        setScrolled(window.scrollY > 40)
        const doc = document.documentElement
        const max = doc.scrollHeight - doc.clientHeight
        if (barRef.current) {
          barRef.current.style.transform = `scaleX(${max > 0 ? window.scrollY / max : 0})`
        }
      })
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      window.removeEventListener('scroll', onScroll)
      // Without this, a scroll event that schedules the rAF right before
      // unmount still fires after, touching a possibly-stale barRef and
      // calling setState on an unmounted component.
      if (raf) cancelAnimationFrame(raf)
    }
  }, [])

  // Lock background scroll while the mobile menu is open, and let Escape
  // close it -- small things a "premium" menu is expected to get right.
  useEffect(() => {
    if (!menuOpen) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = prev
      window.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  return (
    <>
      <div className="nav-progress" ref={barRef} aria-hidden="true" />
      <nav className={`site-nav${scrolled ? ' is-scrolled' : ''}${menuOpen ? ' is-menu-open' : ''}`}>
        <div className="wrap nav-inner">
          <a href="#top" className="nav-brand" data-cursor="link" aria-label="Perch home" onClick={() => setMenuOpen(false)}>
            <PerchMark size={20} />
            <span>PERCH</span>
            {/* The boot sequence's signal dot flies home into this one --
                always mounted (no IntersectionObserver gating) so it's a
                reliable handoff target, and it doubles as a quiet "still
                watching" indicator for the rest of the session. */}
            <span className="nav-live-dot" aria-hidden="true" />
          </a>
          <div className="nav-links">
            <a href="#demo" data-cursor="link">Interface</a>
            <a href={LOGIN_URL} data-cursor="link">Log in</a>
            <a href={SIGNUP_URL} className="nav-cta" data-cursor="cta">Sign up</a>
          </div>
          <button
            className={`nav-burger${menuOpen ? ' is-open' : ''}`}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((o) => !o)}
          >
            <span /><span /><span />
          </button>
        </div>
      </nav>

      <div className={`nav-mobile${menuOpen ? ' is-open' : ''}`} aria-hidden={!menuOpen}>
        <div className="nav-mobile-links">
          {LINKS.map((l, i) => (
            <a
              key={l.href}
              href={l.href}
              data-cursor="link"
              style={{ transitionDelay: `${i * 0.05}s` }}
              onClick={() => setMenuOpen(false)}
              tabIndex={menuOpen ? 0 : -1}
            >
              {l.label}
            </a>
          ))}
          <a
            href={LOGIN_URL}
            data-cursor="link"
            style={{ transitionDelay: `${LINKS.length * 0.05}s` }}
            tabIndex={menuOpen ? 0 : -1}
          >
            Log in
          </a>
        </div>
        <a href={SIGNUP_URL} className="nav-mobile-cta" onClick={() => setMenuOpen(false)} tabIndex={menuOpen ? 0 : -1}>
          Sign up
        </a>
      </div>
    </>
  )
}
