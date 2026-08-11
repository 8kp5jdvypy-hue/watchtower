import { useEffect, useState } from 'react'

export const SESSION_LABEL = { pre: 'PRE-MARKET', open: 'MARKET OPEN', post: 'AFTER HOURS', closed: 'MARKET CLOSED' }

// Real, computed from the actual current time -- not a fabricated
// status. Regular session is 9:30-16:00 ET on weekdays; this
// deliberately doesn't attempt holiday awareness (that would need a
// real calendar source to be honest about), so it's labeled as session
// hours, not a claim that the market is definitely open today.
function sessionState(etDate) {
  const day = etDate.getDay()
  if (day === 0 || day === 6) return 'closed'
  const minutes = etDate.getHours() * 60 + etDate.getMinutes()
  if (minutes < 9 * 60 + 30) return 'pre'
  if (minutes < 16 * 60) return 'open'
  return 'post'
}

export function useMarketClock() {
  const [state, setState] = useState(() => ({ time: '', session: 'closed' }))
  useEffect(() => {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    })
    const tick = () => {
      const now = new Date()
      const etParts = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }))
      setState({ time: fmt.format(now), session: sessionState(etParts) })
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return state
}
