import { useCallback, useMemo, useState } from 'react'
import { api } from '../api'
import { useQuotes } from '../hooks/useQuotes'
import { DEFAULT_POLL_INTERVAL_MS, usePolling } from '../hooks/usePolling'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useMarketClock } from '../hooks/useMarketClock'
import { bySeverity } from '../signalOrder'
import LiveStatus from './LiveStatus'
import PerchMark from './PerchMark'
import SignalCard from './SignalCard'
import SignalDetail from './SignalDetail'
import './Views.css'

export default function Feed() {
  const fetchFeed = useCallback(() => api.signalsFeed(20), [])
  const { data, error, loading, lastSuccessAt } = usePolling(fetchFeed)
  const clock = useMarketClock()
  const liveStatus = useLiveStatus({
    loading, error, hasData: !!data, lastSuccessAt,
    session: clock.session, intervalMs: DEFAULT_POLL_INTERVAL_MS,
  })
  const [openId, setOpenId] = useState(null)
  const symbols = useMemo(() => [...new Set((data?.signals ?? []).map((s) => s.symbol))], [data])
  const quotes = useQuotes(symbols)

  return (
    <div className="view">
      <span className="view-eyebrow"><LiveStatus compact status={liveStatus} session={clock.session} lastSuccessAt={lastSuccessAt} /> SIGNALS</span>
      <h1>Recent activity, across your watchlist and today's radar picks.</h1>
      <p className="view-subtitle">The last 20 HIGH/MEDIUM tier detections.</p>

      {loading && <p className="empty-state">Loading…</p>}
      {error && !data && <p className="empty-state">Couldn't load the feed.</p>}
      {data && data.signals.length === 0 && (
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>No signals yet.</h2>
          <p>Perch is watching. Nothing has crossed the threshold since it started tracking.</p>
        </div>
      )}
      {data && bySeverity(data.signals).map((signal) => (
        <SignalCard key={signal.id} signal={signal} quote={quotes[signal.symbol]} onView={setOpenId} />
      ))}
      {openId && <SignalDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
