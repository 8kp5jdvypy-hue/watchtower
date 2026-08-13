// Money and ET-day helpers for the Trade Journal. The one hard rule
// (see tradebot/api/app.py's _parse_journal_payload): the API speaks
// integer cents, signed, and rejects floats -- so dollars->cents here
// is string arithmetic, never `parseFloat(x) * 100`, which rounds
// 32.57 to 3256.999... and would ship off-by-a-cent bugs.

// "125.5", "-40", "+1,250.00", "$12" -> signed integer cents, or null
// for an empty value. Returns undefined for anything unparseable so
// callers can distinguish "left blank" from "typed something broken".
export function dollarsToCents(input) {
  const raw = String(input ?? '').trim().replace(/[$,\s]/g, '')
  if (raw === '') return null
  const m = raw.match(/^([+-]?)(\d*)(?:\.(\d{0,2}))?$/)
  if (!m || (m[2] === '' && !m[3])) return undefined
  const sign = m[1] === '-' ? -1 : 1
  const whole = parseInt(m[2] || '0', 10)
  const frac = parseInt((m[3] || '').padEnd(2, '0') || '0', 10)
  return sign * (whole * 100 + frac)
}

// U+2212 minus, not a hyphen -- it's full-width and reads as a sign
// next to tabular figures, matching how the landing page sets numbers.
export function formatCents(cents, { sign = false } = {}) {
  if (cents == null) return '—'
  const abs = Math.abs(cents)
  const dollars = (abs / 100).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  if (cents < 0) return `−$${dollars}`
  return `${sign && cents > 0 ? '+' : ''}$${dollars}`
}

// Split form for the two big P&L figures (today's hero tile, the
// detail view): prefix carries the sign and currency mark so CSS can
// set them at the smaller optical size financial type gives marks,
// while the digits stay full-size tabular figures. Exactly the same
// characters formatCents produces -- never a different spelling.
export function formatCentsParts(cents, { sign = false } = {}) {
  if (cents == null) return null
  const abs = Math.abs(cents)
  const dollars = (abs / 100).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  const prefix = cents < 0 ? '−$' : sign && cents > 0 ? '+$' : '$'
  return { prefix, value: dollars }
}

// Calendar-cell version: "+$1.2k" / "−$85" -- a glanceable magnitude,
// not an accounting figure (the row list below the calendar carries
// the exact numbers).
export function formatCentsCompact(cents) {
  if (cents == null) return ''
  if (cents === 0) return '$0' // flat is a fact, not a "+"
  const abs = Math.abs(cents)
  const prefix = cents < 0 ? '−$' : '+$'
  if (abs >= 1_000_00) return `${prefix}${(abs / 1_000_00).toFixed(1).replace(/\.0$/, '')}k`
  return `${prefix}${Math.round(abs / 100)}`
}

export function pnlToneClass(cents) {
  if (cents == null || cents === 0) return 'pnl-flat'
  return cents > 0 ? 'pnl-up' : 'pnl-down'
}

// The API buckets days in US-Eastern (see tradebot/telegram_bot/db.py's
// et_date) -- these helpers only *label* and *key* days the same way,
// they never re-bucket aggregates themselves.
const ET = 'America/New_York'

// en-CA reliably formats as YYYY-MM-DD, which is exactly the API's
// ?date= / calendar-key shape.
const etDayFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit',
})

export function etDateKey(date = new Date()) {
  return etDayFormat.format(date)
}

export function etTime(isoUtc) {
  return `${new Date(isoUtc).toLocaleTimeString('en-US', {
    timeZone: ET, hour: 'numeric', minute: '2-digit',
  })} ET`
}

export function etDateTime(isoUtc) {
  const d = new Date(isoUtc)
  const date = d.toLocaleDateString('en-US', {
    timeZone: ET, weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  })
  return `${date} · ${etTime(isoUtc)}`
}

// toISOString()'s trailing "Z" is valid ISO-8601 but not universally
// parseable by the API's datetime.fromisoformat (older Pythons reject
// "Z"; explicit "+00:00" parses everywhere). Same instant, safer spelling.
export function toApiIso(date) {
  return date.toISOString().replace('Z', '+00:00')
}

export const SOURCE_LABELS = {
  perch_signal: 'Perch signal',
  own_analysis: 'My analysis',
  both: 'Signal + my read',
  other: 'Other',
}
