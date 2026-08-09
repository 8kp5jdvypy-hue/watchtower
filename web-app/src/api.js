// Talks only to tradebot/api/app.py — no business logic here, this is
// pure HTTP plumbing. `credentials: 'include'` on every call is what
// makes the session cookie set by /auth/magic-link/verify actually get
// sent back on subsequent requests (the API and this app are on
// different subdomains — api.perchmarkets.com / app.perchmarkets.com —
// so the browser won't send it without this).
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || `request failed (${status})`)
    this.status = status
    this.body = body
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, body)
  return body
}

export const api = {
  requestMagicLink: (email) => request('/auth/magic-link/request', { method: 'POST', body: JSON.stringify({ email }) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request('/me'),
  watchlist: () => request('/watchlist'),
  signalsToday: () => request('/signals/today'),
  signalsFeed: (limit = 20) => request(`/signals/feed?limit=${limit}`),
  performance: () => request('/performance'),
  activity: () => request('/activity'),
}

export { ApiError }
