import { useCallback, useEffect, useRef, useState } from 'react'
import './JournalOverlay.css'

// The journal's overlay chrome: SignalDetail.jsx's dialog behavior
// (focus trap, Escape, body scroll lock, reverse-animated close,
// centered panel on desktop / bottom sheet on mobile) extracted once so
// TradeSheet and TradeDetail don't each carry their own copy of ~80
// lines of focus plumbing. Visuals live in JournalOverlay.css and
// deliberately mirror SignalDetail.css's .signal-detail so the two
// overlay families in the app read as the same species.
//
// Matches .jo-panel's CSS animation durations -- same JS/CSS sync note
// as SignalDetail.jsx's CLOSE_ANIMATION_MS.
const CLOSE_ANIMATION_MS = 200

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

// `suspended`: true while another overlay (SignalDetail, opened from a
// linked trade) is stacked on top of this one. Both overlays listen for
// Escape at the document level, so without this, one Escape would close
// both layers at once; suspended makes this layer's keyboard handling
// yield until the layer above is gone.
export default function JournalOverlay({ label, eyebrow, onClose, suspended = false, children }) {
  const dialogRef = useRef(null)
  const previouslyFocused = useRef(null)
  const [closing, setClosing] = useState(false)

  const requestClose = useCallback(() => {
    if (prefersReducedMotion()) {
      onClose()
      return
    }
    setClosing(true)
  }, [onClose])

  useEffect(() => {
    if (!closing) return
    const t = setTimeout(onClose, CLOSE_ANIMATION_MS)
    return () => clearTimeout(t)
  }, [closing, onClose])

  useEffect(() => {
    previouslyFocused.current = document.activeElement
    dialogRef.current?.focus()
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
      if (previouslyFocused.current instanceof HTMLElement) previouslyFocused.current.focus()
    }
  }, [])

  useEffect(() => {
    if (suspended) return
    function onKeyDown(e) {
      if (e.key === 'Escape') {
        requestClose()
        return
      }
      if (e.key !== 'Tab' || !dialogRef.current) return
      const focusable = dialogRef.current.querySelectorAll(
        'button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [requestClose, suspended])

  return (
    <div
      className={`jo-overlay${closing ? ' is-closing' : ''}`}
      onMouseDown={(e) => { if (e.target === e.currentTarget) requestClose() }}
    >
      <div
        className={`jo-panel${closing ? ' is-closing' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        ref={dialogRef}
        tabIndex={-1}
      >
        <div className="jo-head">
          <span className="jo-eyebrow">{eyebrow}</span>
          <button type="button" className="jo-close" onClick={requestClose} aria-label={`Close ${label.toLowerCase()}`}>
            Close
          </button>
        </div>
        {/* requestClose passed down so children (Cancel buttons, post-
            submit success) close with the same reverse animation the
            chrome itself uses. */}
        {typeof children === 'function' ? children(requestClose) : children}
      </div>
    </div>
  )
}
