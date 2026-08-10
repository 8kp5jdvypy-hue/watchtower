import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import PerchMark from './components/PerchMark'
import AmbientField from './components/AmbientField'
import Login from './components/Login'
import Today from './components/Today'
import Watchlist from './components/Watchlist'
import Feed from './components/Feed'
import Performance from './components/Performance'
import Activity from './components/Activity'
import './components/PerchMark.css'
import './components/AppShell.css'

// "Feed" -> "Signals" is a rename, not a new view -- same data, clearer
// label. Performance and Activity stay their own honest tabs rather
// than being forced into a "History" label that doesn't quite describe
// either of them. No Settings tab: the only real account-level control
// today is sign-out, which lives in the topbar where it already was --
// inventing a Settings page with nothing real to put in it would be a
// worse result than not having one.
const TABS = [
  { id: 'today', label: 'Today', Component: Today },
  { id: 'watchlist', label: 'Watchlist', Component: Watchlist },
  { id: 'signals', label: 'Signals', Component: Feed },
  { id: 'performance', label: 'Performance', Component: Performance },
  { id: 'activity', label: 'Activity', Component: Activity },
]
// Same five on mobile, no "more" menu -- five compact buttons fits a
// bottom bar fine, and hiding one behind a menu for a beta with a
// handful of users isn't worth the added complexity.

function LoadingShell() {
  return (
    <div className="loading-shell">
      <PerchMark size={28} state="scanning" />
      <span className="loading-label">PERCH</span>
      <span className="loading-sub">Scanning market</span>
    </div>
  )
}

function App() {
  const [account, setAccount] = useState(undefined) // undefined = still checking, null = signed out
  const [activeTab, setActiveTab] = useState('today')

  const checkSession = useCallback(() => {
    api.me().then(setAccount).catch(() => setAccount(null))
  }, [])

  useEffect(() => {
    checkSession()
  }, [checkSession])

  if (account === undefined) {
    return <LoadingShell />
  }

  if (account === null) {
    // Clicking the emailed magic link hits the API directly and ends in
    // a full-page redirect back here (see tradebot/api/app.py's
    // /auth/magic-link/verify) — that reload re-runs checkSession via
    // the effect above, so nothing more is needed here.
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
          <PerchMark size={20} />
          <span>PERCH</span>
          <span className="brand-live" aria-hidden="true" />
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
