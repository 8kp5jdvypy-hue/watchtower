import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import SignalCard from './SignalCard'

export default function Feed() {
  const fetchFeed = useCallback(() => api.signalsFeed(20), [])
  const { data, error, loading } = useApiData(fetchFeed)

  return (
    <div className="view">
      <h1>Recent Signals</h1>
      <p className="view-subtitle">The last 20 HIGH/MEDIUM tier detections, across the whole watchlist.</p>
      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load the feed.</p>}
      {data && data.signals.length === 0 && <p className="empty-state">No signals yet.</p>}
      {data && data.signals.map((signal) => <SignalCard key={signal.id} signal={signal} />)}
    </div>
  )
}
