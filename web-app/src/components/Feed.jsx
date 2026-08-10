import { useCallback, useState } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import PerchMark from './PerchMark'
import SignalCard from './SignalCard'
import SignalDetail from './SignalDetail'
import './Views.css'

export default function Feed() {
  const fetchFeed = useCallback(() => api.signalsFeed(20), [])
  const { data, error, loading } = useApiData(fetchFeed)
  const [openId, setOpenId] = useState(null)

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> SIGNALS</span>
      <h1>Recent activity, across the whole watchlist.</h1>
      <p className="view-subtitle">The last 20 HIGH/MEDIUM tier detections.</p>

      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load the feed.</p>}
      {data && data.signals.length === 0 && (
        <div className="quiet-state">
          <PerchMark size={30} state="idle" />
          <h2>No signals yet.</h2>
          <p>Perch is watching. Nothing has crossed the threshold since it started tracking.</p>
        </div>
      )}
      {data && data.signals.map((signal) => (
        <SignalCard key={signal.id} signal={signal} onView={setOpenId} />
      ))}
      {openId && <SignalDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
