import { useEffect, useRef, useState } from 'react'
import PerchMark from './PerchMark'
import './Nav.css'
import './PerchMark.css'

const LINKS = [
  { href: '#field', label: 'What it watches' },
  { href: '#demo', label: 'Interface' },
  { href: '#coverage', label: 'Markets' },
  { href: '#waitlist', label: 'Access' },
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
    return () => window.removeEventListener('scroll', onScroll)
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
          </a>
          <div className="nav-links">
            <a href="#demo" data-cursor="link">Interface</a>
            <a href="#waitlist" className="nav-cta" data-cursor="cta">Request access</a>
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
        </div>
        <a href="#waitlist" className="nav-mobile-cta" onClick={() => setMenuOpen(false)} tabIndex={menuOpen ? 0 : -1}>
          Request access
        </a>
      </div>
    </>
  )
}
