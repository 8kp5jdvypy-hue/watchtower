// Minimal, anonymous funnel tracking -- see tradebot/funnel_events.py
// for the write path and the reviewed event allowlist. No vendor SDK,
// no cookies set here: anon_id is a random value kept in localStorage,
// sent as the plain-text body of a navigator.sendBeacon POST so it
// (a) survives the page actually navigating away when a CTA is clicked
// and (b) never triggers a CORS preflight, since a "simple" cross-
// origin POST doesn't need one -- see the /events route's own comment
// in tradebot/api/app.py for why that matters for a static site with
// no other reason to talk to api.perchmarkets.com.
const API_URL = 'https://api.perchmarkets.com'
const STORAGE_KEY = 'perch_anon_id'

function randomId() {
  if (crypto.randomUUID) return crypto.randomUUID()
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`
}

export function getAnonId() {
  try {
    let id = localStorage.getItem(STORAGE_KEY)
    if (!id) {
      id = randomId()
      localStorage.setItem(STORAGE_KEY, id)
    }
    return id
  } catch {
    return 'no-storage'
  }
}

export function track(event, props) {
  const body = JSON.stringify({ event, anon_id: getAnonId(), props })
  try {
    if (navigator.sendBeacon && navigator.sendBeacon(`${API_URL}/events`, body)) return
  } catch { /* fall through */ }
  fetch(`${API_URL}/events`, { method: 'POST', body, mode: 'no-cors', keepalive: true }).catch(() => {})
}

// Appends the current anon_id as a URL param so app.perchmarkets.com
// can adopt the same id (see web-app/src/analytics.js's getAnonId) and
// keep tracing the same visitor across the domain handoff, without
// cookies or any shared-domain config on either Cloudflare Worker.
export function withRef(url) {
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}ref=${encodeURIComponent(getAnonId())}`
}
