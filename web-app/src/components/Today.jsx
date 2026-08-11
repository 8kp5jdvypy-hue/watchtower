import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { DEFAULT_POLL_INTERVAL_MS, usePolling } from '../hooks/usePolling'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useMarketClock } from '../hooks/useMarketClock'
import { useQuotes } from '../hooks/useQuotes'
import { bySeverity } from '../signalOrder'
import LiveStatus from './LiveStatus'
import PerchMark from './PerchMark'
import SignalCard from './SignalCard'
import SignalDetail from './SignalDetail'
import './Views.css'
import './WelcomeBanner.css'
import './SignalArrival.css'

// How long a just-arrived card keeps its arrival treatment (see
// SignalArrival.css's signal-arrival-glow) before the wrapper class is
// dropped -- kept a little longer than the CSS animation itself so the
// glow never gets cut off mid-fade by a re-render.
const ARRIVAL_HIGHLIGHT_MS = 1800

function onboardingKey(accountId) {
  return `perch_onboarded_${accountId}`
}

export default function Today({ account }) {
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading, lastSuccessAt } = usePolling(fetchToday)
  const clock = useMarketClock()
  const liveStatus = useLiveStatus({
    loading, error, hasData: !!data, lastSuccessAt,
    session: clock.session, intervalMs: DEFAULT_POLL_INTERVAL_MS,
  })
  const [openId, setOpenId] = useState(null)
  const symbols = useMemo(() => [...new Set((data?.signals ?? []).map((s) => s.symbol))], [data])
  const quotes = useQuotes(symbols)

  // Tracks which signal IDs are genuinely new since the LAST poll, not
  // just new to this component instance -- the initial load is a batch,
  // not an arrival, so nothing animates as "just arrived" until a second
  // poll actually adds something to what was already there.
  const seenIdsRef = useRef(null)
  const arrivalTimersRef = useRef(new Map())
  const [arrivedIds, setArrivedIds] = useState(() => new Set())

  useEffect(() => {
    if (!data) return
    const currentIds = new Set(data.signals.map((s) => s.id))
    if (seenIdsRef.current === null) {
      seenIdsRef.current = currentIds
      return
    }
    const newlyArrived = [...currentIds].filter((id) => !seenIdsRef.current.has(id))
    seenIdsRef.current = currentIds
    if (newlyArrived.length === 0) return
    setArrivedIds((prev) => new Set([...prev, ...newlyArrived]))
    newlyArrived.forEach((id) => {
      const timer = setTimeout(() => {
        setArrivedIds((prev) => {
          if (!prev.has(id)) return prev
          const next = new Set(prev)
          next.delete(id)
          return next
        })
        arrivalTimersRef.current.delete(id)
      }, ARRIVAL_HIGHLIGHT_MS)
      arrivalTimersRef.current.set(id, timer)
    })
  }, [data])

  useEffect(() => {
    const timers = arrivalTimersRef.current
    return () => timers.forEach(clearTimeout)
  }, [])

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
      <LiveStatus status={liveStatus} session={clock.session} time={clock.time} lastSuccessAt={lastSuccessAt} />
      <h1>What deserves your attention right now.</h1>
      {signalCount > 0 && (
        <p className="view-subtitle">
          {signalCount} thing{signalCount === 1 ? '' : 's'} worth knowing this session
        </p>
      )}

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
              <p>Perch is watching your full watchlist, plus scanning the market for anything else worth flagging. The moment something unusual happens, a card like the ones below will show up here.</p>
            )}
          </div>
          <button type="button" className="welcome-banner-dismiss" onClick={dismissWelcome}>Got it</button>
        </div>
      )}

      {loading && <p className="empty-state">Loading…</p>}
      {error && !data && <p className="empty-state">Couldn't load today's signals.</p>}
      {data && data.signals.length === 0 && (
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>Watching the market.</h2>
          <p>Nothing HIGH or MEDIUM tier has crossed the threshold yet today. That's not a bug — it's Perch deciding there's nothing worth interrupting you for.</p>
        </div>
      )}
      {data && bySeverity(data.signals).map((signal) => (
        arrivedIds.has(signal.id) ? (
          <div className="signal-arrival" key={signal.id}>
            <SignalCard signal={signal} quote={quotes[signal.symbol]} onView={setOpenId} />
          </div>
        ) : (
          <SignalCard key={signal.id} signal={signal} quote={quotes[signal.symbol]} onView={setOpenId} />
        )
      ))}
      {openId && <SignalDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
