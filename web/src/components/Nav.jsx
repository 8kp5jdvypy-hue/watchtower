import { useEffect, useRef, useState } from 'react'
import PerchMark from './PerchMark'
import './Nav.css'
import './PerchMark.css'

export default function Nav() {
  const [scrolled, setScrolled] = useState(false)
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

  return (
    <>
      <div className="nav-progress" ref={barRef} aria-hidden="true" />
      <nav className={`site-nav${scrolled ? ' is-scrolled' : ''}`}>
        <div className="wrap nav-inner">
          <a href="#top" className="nav-brand" data-cursor="link" aria-label="Perch home">
            <PerchMark size={20} />
            <span>PERCH</span>
          </a>
          <div className="nav-links">
            <a href="#demo" data-cursor="link">Interface</a>
            <a href="#waitlist" className="nav-cta" data-cursor="cta">Request access</a>
          </div>
        </div>
      </nav>
    </>
  )
}
