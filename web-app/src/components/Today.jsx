import { useCallback } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import SignalCard from './SignalCard'

export default function Today() {
  const fetchToday = useCallback(() => api.signalsToday(), [])
  const { data, error, loading } = useApiData(fetchToday)

  return (
    <div className="view">
      <h1>Today</h1>
      <p className="view-subtitle">
        {data ? `Session ${data.session} — 3 things worth knowing` : '3 things worth knowing'}
      </p>
      {loading && <p className="empty-state">Loading…</p>}
      {error && <p className="empty-state">Couldn't load today's signals.</p>}
      {data && data.signals.length === 0 && (
        <p className="empty-state">Nothing HIGH or MEDIUM tier has fired yet today.</p>
      )}
      {data && data.signals.map((signal) => <SignalCard key={signal.id} signal={signal} />)}
    </div>
  )
}
