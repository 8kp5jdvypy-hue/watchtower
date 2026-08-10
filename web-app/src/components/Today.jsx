import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { useMarketClock } from '../hooks/useMarketClock'
import PerchMark from './PerchMark'
import SignalCard from './SignalCard'
import './Views.css'

const SESSION_LABEL = { pre: 'PRE-MARKET', open: 'MARKET OPEN', post: 'AFTER HOURS', closed: 'MARKET CLOSED' }

export default function Today() {
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading } = useApiData(fetchToday)
  const clock = useMarketClock()

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> PERCH / LIVE</span>
      <h1>What deserves your attention right now.</h1>
      <p className="view-subtitle">
        {clock.time ? `${SESSION_LABEL[clock.session]} — ${clock.time} ET` : 'Loading market status…'}
        {data && ` · 3 things worth knowing this session`}
      </p>

      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load today's signals.</p>}
      {data && data.signals.length === 0 && (
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>Watching the market.</h2>
          <p>Nothing HIGH or MEDIUM tier has crossed the threshold yet today. That's not a bug — it's Perch deciding there's nothing worth interrupting you for.</p>
        </div>
      )}
      {data && data.signals.map((signal) => <SignalCard key={signal.id} signal={signal} />)}
    </div>
  )
}
