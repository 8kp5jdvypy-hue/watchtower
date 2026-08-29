// Talks only to tradebot/api/app.py — no business logic here, this is
// pure HTTP plumbing. `credentials: 'include'` on every call is what
// makes the session cookie set by /auth/magic-link/verify actually get
// sent back on subsequent requests (the API and this app are on
// different subdomains — api.perchmarkets.com / app.perchmarkets.com —
// so the browser won't send it without this).
const API_URL = import.meta.env?.VITE_API_URL || 'http://localhost:8000'
export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || `request failed (${status})`)
    this.status = status
    this.body = body
  }
}

class ApiProtocolError extends Error {
  constructor(status, cause = null) {
    super(`server returned an invalid response (${status})`)
    this.name = 'ApiProtocolError'
    this.status = status
    this.cause = cause
  }
}

class ApiTimeoutError extends Error {
  constructor(timeoutMs) {
    super(`request timed out after ${timeoutMs}ms`)
    this.name = 'ApiTimeoutError'
    this.timeoutMs = timeoutMs
  }
}

export async function parseApiResponse(response) {
  let body
  try {
    body = await response.json()
  } catch (error) {
    if (response.ok) throw new ApiProtocolError(response.status, error)
    throw new ApiError(response.status, null)
  }
  if (!response.ok) throw new ApiError(response.status, body)
  // Every successful Perch endpoint returns a JSON object. A syntactically
  // valid `null`/primitive is still a broken application contract and must
  // become an explicit error rather than looking like "still loading."
  if (body === null || typeof body !== 'object') {
    throw new ApiProtocolError(response.status)
  }
  return body
}

export async function request(path, options = {}) {
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchOptions } = options
  const controller = new AbortController()
  let timedOut = false
  const timeoutId = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  try {
    const response = await fetch(`${API_URL}${path}`, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      ...fetchOptions,
      signal: controller.signal,
    })
    return await parseApiResponse(response)
  } catch (error) {
    if (timedOut) throw new ApiTimeoutError(timeoutMs)
    throw error
  } finally {
    clearTimeout(timeoutId)
  }
}

export const api = {
  requestMagicLink: (email) => request('/auth/magic-link/request', { method: 'POST', body: JSON.stringify({ email }) }),
  // POST, not the token-in-a-GET the emailed link used to hit directly --
  // see VerifyMagicLink.jsx for why. request()'s default
  // Content-Type: application/json is load-bearing here, not incidental:
  // it's what forces the CORS preflight that keeps this un-forgeable
  // from any origin other than this app.
  verifyMagicLink: (token) => request('/auth/magic-link/verify', { method: 'POST', body: JSON.stringify({ token }) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request('/me'),
  watchlist: () => request('/watchlist'),
  signalsToday: () => request('/signals/today'),
  signalsFeed: (limit = 20) => request(`/signals/feed?limit=${limit}`),
  signalDetail: (id) => request(`/signals/${encodeURIComponent(id)}`),
  performance: () => request('/performance'),
  activity: () => request('/activity'),
  // Restricted server-side to the account's own watchlist -- an empty
  // or all-outside-watchlist symbols list just returns {quotes: {}}.
  quotes: (symbols) => request(`/quotes?symbols=${symbols.map(encodeURIComponent).join(',')}`),

  // Trade Journal (Phase 3) -- pnl_cents is always integer cents, signed;
  // the dollars<->cents conversion lives in journalFormat.js, never here.
  // Day bucketing (calendar keys, ?date=) is US-Eastern and done by the
  // API -- the client never re-buckets timestamps into days itself.
  journalSummary: () => request('/journal/summary'),
  journalCalendar: (month) => request(`/journal/calendar?month=${encodeURIComponent(month)}`),
  journalTrades: (date) => request(date ? `/journal/trades?date=${encodeURIComponent(date)}` : '/journal/trades'),
  journalCreateTrade: (payload) => request('/journal/trades', { method: 'POST', body: JSON.stringify(payload) }),
  journalUpdateTrade: (id, payload) =>
    request(`/journal/trades/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  journalDeleteTrade: (id) => request(`/journal/trades/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // The alerts this account was actually sent (outbox delivery log, not
  // the global feed) -- delivery_history: false means no linked Telegram,
  // which the UI renders as nothing at all, never an upsell.
  journalLinkableSignals: (symbol) =>
    request(symbol ? `/journal/linkable-signals?symbol=${encodeURIComponent(symbol)}` : '/journal/linkable-signals'),
}

// A plain top-level navigation, not a fetch: the browser downloads the
// CSV with the session cookie attached (SameSite=Lax allows top-level
// GET navigations cross-subdomain, and CORS doesn't apply to
// navigations) -- no blob plumbing needed.
export const JOURNAL_EXPORT_URL = `${API_URL}/journal/export.csv`

export { ApiError, ApiProtocolError, ApiTimeoutError }
