import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { track } from './analytics'
import { SESSION_LABEL, useMarketClock } from './hooks/useMarketClock'
import PerchMark from './components/PerchMark'
import AmbientField from './components/AmbientField'
import Login from './components/Login'
import VerifyMagicLink from './components/VerifyMagicLink'
import Today from './components/Today'
import Watchlist from './components/Watchlist'
import Feed from './components/Feed'
import Journal from './components/Journal'
import Performance from './components/Performance'
import Activity from './components/Activity'
import Settings from './components/Settings'
import './components/PerchMark.css'
import './components/AppShell.css'

// "Feed" -> "Signals" is a rename, not a new view -- same data, clearer
// label. Performance and Activity stay their own honest tabs rather
// than being forced into a "History" label that doesn't quite describe
// either of them. Settings now exists because there's something real
// to put in it: Telegram alert setup info and the plan/subscription
// preview -- see Settings.jsx.
const TABS = [
  { id: 'today', label: 'Today', Component: Today },
  { id: 'watchlist', label: 'Watchlist', Component: Watchlist },
  { id: 'signals', label: 'Signals', Component: Feed },
  // Journal sits right after Signals on purpose: "what Perch saw" then
  // "what I did about it" -- a primary destination, not a history tab.
  { id: 'journal', label: 'Journal', Component: Journal },
  { id: 'performance', label: 'Performance', Component: Performance },
  { id: 'activity', label: 'Activity', Component: Activity },
  { id: 'settings', label: 'Settings', Component: Settings },
]
// All seven on mobile, still no "more" menu. Six equal-width buttons was
// the max at 360px; seven fit because .mobile-nav-button now sizes to
// its label (flex: 1 1 auto) instead of forcing seven equal columns --
// "Performance" gets the width it needs, "Today" stops hoarding the
// width it doesn't -- plus slightly tighter type. Verified no wrapping
// or truncation at 360px; see AppShell.css's .mobile-nav-button.

function LoadingShell() {
  return (
    <div className="loading-shell">
      <PerchMark size={28} state="scanning" />
      <span className="loading-label">PERCH</span>
      <span className="loading-sub">Scanning market</span>
    </div>
  )
}

function getMagicLinkToken() {
  return new URLSearchParams(window.location.search).get('token')
}

function App() {
  const [account, setAccount] = useState(undefined) // undefined = still checking, null = signed out
  const [activeTab, setActiveTab] = useState('today')
  // Set from the URL once, at mount -- cleared (see handleVerified) once
  // it's been used, never re-derived from the URL again, so nothing
  // re-shows the confirm screen for a token that's already been spent.
  const [magicLinkToken, setMagicLinkToken] = useState(getMagicLinkToken)
  const trackedAuthRef = useRef(false)
  const clock = useMarketClock()

  const checkSession = useCallback(() => {
    api.me().then(setAccount).catch(() => setAccount(null))
  }, [])

  useEffect(() => {
    checkSession()
  }, [checkSession])

  function handleVerified() {
    const params = new URLSearchParams(window.location.search)
    params.delete('token')
    const rest = params.toString()
    window.history.replaceState(null, '', rest ? `${window.location.pathname}?${rest}` : window.location.pathname)
    setMagicLinkToken(null)
    checkSession()
  }

  // Fires once per real session, not on every re-render or tab switch --
  // this is "a signed-in session exists," the funnel's last real step,
  // not a page-view counter (see tradebot/funnel_events.py's ALLOWED_EVENTS).
  useEffect(() => {
    if (account && !trackedAuthRef.current) {
      trackedAuthRef.current = true
      track('app_authenticated')
    }
  }, [account])

  // Checked before the loading/signed-out branches below on purpose: a
  // token in the URL means "not signed in yet, about to be" regardless
  // of where checkSession's own request happens to be — showing the
  // confirm button immediately avoids a loading-spinner-then-Login
  // flash before it. Skipped once `account` actually resolves truthy
  // (an already-valid session hit this URL with a stale token attached)
  // so a real session is never held hostage behind a dead link.
  if (magicLinkToken && !account) {
    return <VerifyMagicLink token={magicLinkToken} onVerified={handleVerified} />
  }

  if (account === undefined) {
    return <LoadingShell />
  }

  if (account === null) {
    // The emailed link lands on VerifyMagicLink above, not here directly
    // — see tradebot/api/app.py's /auth/magic-link/verify, which is a
    // same-origin POST now, not a bare GET a passive page load could
    // trigger. handleVerified re-runs checkSession once that POST
    // succeeds, which is what gets a signed-in visitor out of Login.
    return <Login />
  }

  const handleLogout = () => {
    api.logout().finally(() => setAccount(null))
  }

  const ActiveComponent = TABS.find((t) => t.id === activeTab).Component

  return (
    <div className="app-shell">
      <AmbientField />
      <div className="topbar">
        <div className="brand">
          <PerchMark size={20} state={clock.session === 'open' ? 'scanning' : 'idle'} />
          <span>PERCH</span>
          <span
            className={`brand-live brand-live-${clock.session}`}
            title={`${SESSION_LABEL[clock.session]}${clock.time ? ` — ${clock.time} ET` : ''}`}
            aria-hidden="true"
          />
        </div>
        <div className="topbar-account">
          <span>{account.email || 'linked via Telegram'}</span>
          <button className="logout-button" onClick={handleLogout}>Sign out</button>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={tab.id === activeTab}
            className={`tab-button${tab.id === activeTab ? ' active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <ActiveComponent account={account} />
      <nav className="mobile-nav" aria-label="Primary">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`mobile-nav-button${tab.id === activeTab ? ' active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
            aria-current={tab.id === activeTab ? 'page' : undefined}
          >
            {tab.label}
          </button>
        ))}
      </nav>
    </div>
  )
}

export default App
