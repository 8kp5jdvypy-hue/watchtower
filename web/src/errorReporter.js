// Minimal production error visibility -- see tradebot/client_errors.py
// for the write path. No vendor SDK (no Sentry/Bugsnag): the smallest
// thing that answers "is anything actually breaking for a real
// visitor" when nobody's watching a console. Same sendBeacon pattern
// as analytics.js, for the same reason (survives the page unloading).
const API_URL = 'https://api.perchmarkets.com'

export function reportError(message, stack, url) {
  const body = JSON.stringify({
    message: String(message || 'Unknown error'),
    stack: stack ? String(stack) : undefined,
    url: url || window.location.href,
  })
  try {
    if (navigator.sendBeacon && navigator.sendBeacon(`${API_URL}/client-errors`, body)) return
  } catch { /* fall through */ }
  fetch(`${API_URL}/client-errors`, { method: 'POST', body, mode: 'no-cors', keepalive: true }).catch(() => {})
}

// Catches script errors and unhandled promise rejections outside
// React's render cycle -- a render crash is handled separately by
// ErrorBoundary.jsx, since window.onerror never sees those. Genuinely
// relevant here specifically: the hero is a WebGL scene, and context
// creation/shader compilation failures on an unsupported or
// GPU-blacklisted browser are a real, not hypothetical, failure mode.
export function installGlobalErrorReporting() {
  window.addEventListener('error', (event) => {
    reportError(event.message, event.error?.stack)
  })
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    const message = reason instanceof Error ? reason.message : String(reason)
    const stack = reason instanceof Error ? reason.stack : undefined
    reportError(`Unhandled rejection: ${message}`, stack)
  })
}
