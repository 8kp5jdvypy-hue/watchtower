import { useCallback, useState } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { useMarketClock } from '../hooks/useMarketClock'
import PerchMark from './PerchMark'
import SignalCard from './SignalCard'
import SignalDetail from './SignalDetail'
import './Views.css'
import './WelcomeBanner.css'

const SESSION_LABEL = { pre: 'PRE-MARKET', open: 'MARKET OPEN', post: 'AFTER HOURS', closed: 'MARKET CLOSED' }

function onboardingKey(accountId) {
  return `perch_onboarded_${accountId}`
}

export default function Today({ account }) {
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading } = useApiData(fetchToday)
  const clock = useMarketClock()
  const [openId, setOpenId] = useState(null)

  // Shown once per account, on this browser -- the first time Today
  // ever renders with real data, not a multi-step tour, just enough
  // context that a brand-new signed-in user understands what they're
  // looking at before this tab settles into whatever it would normally
  // show. Keyed by account.id (not a single global flag) so a shared
  // browser or a second account on the same device gets its own first
  // look. Uses a real signal when one exists today; when the session is
  // quiet, it says so honestly rather than inventing one -- same
  // discipline as the quiet-state below.
  const [dismissed, setDismissed] = useState(() => {
    try {
      return account?.id ? localStorage.getItem(onboardingKey(account.id)) === '1' : true
    } catch {
      return true
    }
  })

  function dismissWelcome() {
    setDismissed(true)
    try {
      if (account?.id) localStorage.setItem(onboardingKey(account.id), '1')
    } catch { /* ignore */ }
  }

  const showWelcome = !dismissed && !loading && !error && !!data
  const topSignal = data?.signals?.[0]
  const signalCount = data?.signals?.length ?? 0

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> PERCH / LIVE</span>
      <h1>What deserves your attention right now.</h1>
      <p className="view-subtitle">
        {clock.time ? `${SESSION_LABEL[clock.session]} — ${clock.time} ET` : 'Loading market status…'}
        {signalCount > 0 && ` · ${signalCount} thing${signalCount === 1 ? '' : 's'} worth knowing this session`}
      </p>

      {showWelcome && (
        <div className="welcome-banner">
          <PerchMark size={22} state={topSignal ? 'confirmed' : 'idle'} />
          <div className="welcome-banner-body">
            <h2>Welcome to Perch.</h2>
            {topSignal ? (
              <p>
                <b>{topSignal.symbol}</b> below is a real signal Perch caught today — tap "View signal" on any card to see why Perch noticed.
              </p>
            ) : (
              <p>Perch is watching SPY, QQQ, GOOGL, TSLA, BE, IONQ right now. The moment something unusual happens, a card like the ones below will show up here.</p>
            )}
          </div>
          <button type="button" className="welcome-banner-dismiss" onClick={dismissWelcome}>Got it</button>
        </div>
      )}

      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load today's signals.</p>}
      {data && data.signals.length === 0 && (
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>Watching the market.</h2>
          <p>Nothing HIGH or MEDIUM tier has crossed the threshold yet today. That's not a bug — it's Perch deciding there's nothing worth interrupting you for.</p>
        </div>
      )}
      {data && data.signals.map((signal) => (
        <SignalCard key={signal.id} signal={signal} onView={setOpenId} />
      ))}
      {openId && <SignalDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
