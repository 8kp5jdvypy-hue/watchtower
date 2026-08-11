// Minimal production error visibility -- see tradebot/client_errors.py
// for the write path. No vendor SDK (no Sentry/Bugsnag): every bug
// found on this project so far was caught by a person manually
// watching a browser console during a design pass, and this is the
// smallest thing that closes that gap for real users. Same sendBeacon
// pattern as analytics.js, for the same reason (survives the page
// unloading, e.g. right after a crash triggers a reload).
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

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

// Catches script errors and unhandled promise rejections that happen
// outside React's render cycle -- a React render crash is handled
// separately by ErrorBoundary.jsx, since window.onerror never sees those.
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
