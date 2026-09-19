// The site's only data: Perch's own public record and its live status.
// Both are real endpoints on the production API; nothing here is
// illustrative. If either fails, the components render their honest
// empty state -- never a placeholder number.
const API_URL = 'https://api.perchmarkets.com'

const ET = 'America/New_York'

export function etParts(iso) {
  const d = new Date(iso)
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: ET, hour12: false, hour: '2-digit', minute: '2-digit', weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  })
  const parts = Object.fromEntries(fmt.formatToParts(d).map((p) => [p.type, p.value]))
  const hour = Number(parts.hour) % 24
  const minute = Number(parts.minute)
  return {
    hour, minute,
    minutesOfDay: hour * 60 + minute,
    time: `${String(hour).padStart(2, '0')}:${parts.minute}`,
    date: `${parts.weekday} ${parts.month} ${parts.day}`,
    dateKey: new Intl.DateTimeFormat('en-CA', { timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d),
  }
}

/** The session the page walks through: the most recent one whose
 * outcomes the journal has already recorded (the close batch marks a
 * session after it ends, so today's alerts are "pending" until then).
 * Falls back to the newest session if none is scored yet. `pending`
 * says whether a newer, still-unmarked session exists. Oldest first. */
export function latestSession(alerts) {
  if (!alerts?.length) return { dateKey: null, alerts: [], pending: false }
  const byDay = new Map()
  for (const a of alerts) {
    const key = etParts(a.sent_at).dateKey
    if (!byDay.has(key)) byDay.set(key, [])
    byDay.get(key).push(a)
  }
  const keys = [...byDay.keys()].sort()
  const scored = keys.filter((k) => byDay.get(k).some((a) => a.return_pct != null))
  const dateKey = scored.at(-1) ?? keys.at(-1)
  const list = byDay.get(dateKey).slice().sort((a, b) => a.sent_at.localeCompare(b.sent_at))
  return { dateKey, alerts: list, pending: dateKey !== keys.at(-1) }
}

export async function fetchTrackRecord(signal) {
  const res = await fetch(`${API_URL}/public/track-record`, { signal })
  if (!res.ok) throw new Error(`track-record ${res.status}`)
  return res.json()
}

export async function fetchStatus(signal) {
  const res = await fetch(`${API_URL}/v1/status`, { signal })
  if (!res.ok) throw new Error(`status ${res.status}`)
  return res.json()
}

export const MARKET_STATE_LABEL = {
  premarket: 'Premarket',
  open: 'Market open',
  after_hours: 'After hours',
  closed: 'Market closed',
  holiday: 'Exchange holiday',
}

/** A real alert's outcome, in words the record supports. */
export function outcomeOf(alert) {
  if (alert.return_pct == null) return { kind: 'pending', label: 'outcome pending' }
  const pct = alert.return_pct
  const dir = alert.trend === 'down' ? -1 : 1
  const continued = pct * dir > 0
  return {
    kind: continued ? 'continued' : 'reversed',
    label: `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% at ${alert.offset_min} min · ${continued ? 'continued' : 'reversed'}`,
  }
}
