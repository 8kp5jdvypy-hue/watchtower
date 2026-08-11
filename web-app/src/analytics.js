// Minimal, anonymous funnel tracking -- see tradebot/funnel_events.py
// for the write path and the reviewed event allowlist. No vendor SDK,
// no cookies set here: anon_id is a random value kept in localStorage.
// If the visitor arrived from a perchmarkets.com CTA (see
// web/src/analytics.js's withRef), a `?ref=` query param carries that
// same id over so a signup funnel can be traced landing -> app without
// any shared-domain cookie config between the two Cloudflare Workers.
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const STORAGE_KEY = 'perch_anon_id'

function randomId() {
  if (crypto.randomUUID) return crypto.randomUUID()
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`
}

export function getAnonId() {
  try {
    const ref = new URLSearchParams(window.location.search).get('ref')
    if (ref) {
      localStorage.setItem(STORAGE_KEY, ref)
      return ref
    }
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
