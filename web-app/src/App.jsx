import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import Login from './components/Login'
import Today from './components/Today'
import Watchlist from './components/Watchlist'
import Feed from './components/Feed'
import Performance from './components/Performance'
import Activity from './components/Activity'

const TABS = [
  { id: 'today', label: 'Today', Component: Today },
  { id: 'watchlist', label: 'Watchlist', Component: Watchlist },
  { id: 'feed', label: 'Recent Signals', Component: Feed },
  { id: 'performance', label: 'Performance', Component: Performance },
  { id: 'activity', label: 'My Activity', Component: Activity },
]

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
    return <div className="loading-shell">Loading…</div>
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
      <div className="topbar">
        <div className="brand">Perch</div>
        <div className="topbar-account">
          <span>{account.email || 'linked via Telegram'}</span>
          <button className="logout-button" onClick={handleLogout}>Sign out</button>
        </div>
      </div>
      <div className="tabs">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`tab-button${tab.id === activeTab ? ' active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <ActiveComponent account={account} />
    </div>
  )
}

export default App
