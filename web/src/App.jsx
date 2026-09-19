import { useEffect, useState } from 'react'
import Nav from './components/Nav'
import SessionRail from './components/SessionRail'
import Hero from './components/Hero'
import Anatomy from './components/Anatomy'
import SessionLog from './components/SessionLog'
import Boundary from './components/Boundary'
import Record from './components/Record'
import TheApp from './components/TheApp'
import Access from './components/Access'
import Footer from './components/Footer'
import ErrorBoundary from './components/ErrorBoundary'
import { etParts, fetchStatus, fetchTrackRecord, latestSession } from './lib/perchData'

/*
 * perchmarkets.com — "The Watch Log" (DESIGN-DIRECTION-2026-09.md).
 * One continuous page structured as a trading session; the session
 * rail on the left advances through the day as you scroll and carries
 * the latest session's real alerts. Two fetches, both public, both
 * real; everything renders honestly without them.
 */
export default function App() {
  const [record, setRecord] = useState(null)
  const [status, setStatus] = useState(null)
  useEffect(() => {
    const ac = new AbortController()
    fetchTrackRecord(ac.signal).then(setRecord).catch(() => setRecord(null))
    fetchStatus(ac.signal).then(setStatus).catch(() => setStatus(null))
    const tick = setInterval(() => fetchStatus(ac.signal).then(setStatus).catch(() => {}), 60_000)
    return () => { ac.abort(); clearInterval(tick) }
  }, [])
  const session = latestSession(record?.alerts)
  const sessionLabel = session.alerts.length ? etParts(session.alerts[0].sent_at).date : 'Session'
  return (
    <ErrorBoundary>
      <a className="skip-link" href="#main">Skip to content</a>
      <Nav status={status} />
      <SessionRail alerts={session.alerts} sessionLabel={sessionLabel} />
      <main id="main" className="page">
        <Hero status={status} record={record} />
        <Anatomy record={record} />
        <SessionLog record={record} />
        <Boundary />
        <Record record={record} />
        <TheApp />
        <Access />
        <Footer status={status} />
      </main>
    </ErrorBoundary>
  )
}
