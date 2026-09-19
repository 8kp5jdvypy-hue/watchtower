import { useEffect, useState } from 'react'
import { PerchLockup } from './PerchMark'
import { LOGIN_URL, SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './Nav.css'

const LINKS = [
  { href: '#reads', label: 'How it reads' },
  { href: '#session', label: 'The session' },
  { href: '#record', label: 'Record' },
  { href: '#app', label: 'iPhone' },
]

export default function Nav({ status }) {
  const [open, setOpen] = useState(false)
  // The mark blooms once on arrival, then shows 'signal' while the
  // scanner is live in an open session -- the same flag the product uses.
  const [arrived, setArrived] = useState(false)
  useEffect(() => { const t = setTimeout(() => setArrived(true), 900); return () => clearTimeout(t) }, [])
  const live = status?.market?.state === 'open' && status?.sources?.[0]?.status === 'operational'
  const markState = !arrived ? 'confirmed' : live ? 'signal' : 'idle'
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])
  return (
    <header className="nav">
      <div className="nav-inner">
        <a href="#top" className="nav-brand" aria-label="Perch home" onClick={() => setOpen(false)}>
          <PerchLockup size={22} state={markState} />
        </a>
        <nav className="nav-links" aria-label="Sections">
          {LINKS.map((l) => <a key={l.href} href={l.href}>{l.label}</a>)}
        </nav>
        <div className="nav-actions">
          <a href={withRef(LOGIN_URL)} className="nav-login" onClick={() => track('login_cta_click', { source: 'nav' })}>Sign in</a>
          <a href={withRef(SIGNUP_URL)} className="nav-cta" onClick={() => track('signup_cta_click', { source: 'nav' })}>Get access</a>
        </div>
        <button className="nav-burger" aria-expanded={open} aria-controls="nav-sheet" onClick={() => setOpen((v) => !v)}>
          <span className="sr-only">{open ? 'Close menu' : 'Open menu'}</span>
          <span aria-hidden="true" className={`nav-burger-lines ${open ? 'is-open' : ''}`} />
        </button>
      </div>
      <div id="nav-sheet" className={`nav-sheet ${open ? 'is-open' : ''}`} hidden={!open}>
        {LINKS.map((l) => <a key={l.href} href={l.href} onClick={() => setOpen(false)}>{l.label}</a>)}
        <a href={withRef(LOGIN_URL)} onClick={() => track('login_cta_click', { source: 'nav_sheet' })}>Sign in</a>
        <a href={withRef(SIGNUP_URL)} className="nav-cta" onClick={() => track('signup_cta_click', { source: 'nav_sheet' })}>Get access</a>
      </div>
    </header>
  )
}
