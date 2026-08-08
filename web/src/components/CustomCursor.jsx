import { useEffect, useRef } from 'react'
import { useFinePointer, useReducedMotion } from '../hooks/usePrefs'
import './CustomCursor.css'

// Elements opt in via data-cursor="link" | "data" | "cta".
// Everything else gets the plain small dot.
export default function CustomCursor() {
  const fine = useFinePointer()
  const reduced = useReducedMotion()
  const dotRef = useRef(null)
  const ringRef = useRef(null)
  const active = fine && !reduced

  useEffect(() => {
    if (!active) return
    document.documentElement.classList.add('has-custom-cursor')
    const dot = dotRef.current
    const ring = ringRef.current
    let mx = window.innerWidth / 2
    let my = window.innerHeight / 2
    let rx = mx
    let ry = my
    let raf

    const onMove = (e) => {
      mx = e.clientX
      my = e.clientY
      dot.style.transform = `translate(${mx}px,${my}px) translate(-50%,-50%)`
    }
    const loop = () => {
      rx += (mx - rx) * 0.18
      ry += (my - ry) * 0.18
      ring.style.transform = `translate(${rx.toFixed(1)}px,${ry.toFixed(1)}px) translate(-50%,-50%)`
      raf = requestAnimationFrame(loop)
    }
    const onOver = (e) => {
      const el = e.target.closest('[data-cursor]')
      ring.dataset.mode = el ? el.dataset.cursor : ''
    }
    window.addEventListener('pointermove', onMove, { passive: true })
    document.addEventListener('pointerover', onOver)
    raf = requestAnimationFrame(loop)

    return () => {
      document.documentElement.classList.remove('has-custom-cursor')
      window.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerover', onOver)
      cancelAnimationFrame(raf)
    }
  }, [active])

  if (!active) return null
  return (
    <>
      <div className="cx-dot" ref={dotRef} aria-hidden="true" />
      <div className="cx-ring" ref={ringRef} aria-hidden="true">
        <span className="cx-signal" />
      </div>
    </>
  )
}
