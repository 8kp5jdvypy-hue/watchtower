import { useRef } from 'react'
import { useFinePointer, useReducedMotion } from '../hooks/usePrefs'
import './MagneticButton.css'

const RADIUS = 90
const STRENGTH = 0.4

export default function MagneticButton({ as: Tag = 'button', className = '', children, ...props }) {
  const ref = useRef(null)
  const fine = useFinePointer()
  const reduced = useReducedMotion()
  const active = fine && !reduced

  function onMove(e) {
    if (!active || !ref.current) return
    const r = ref.current.getBoundingClientRect()
    const cx = r.left + r.width / 2
    const cy = r.top + r.height / 2
    const dx = e.clientX - cx
    const dy = e.clientY - cy
    const dist = Math.hypot(dx, dy)
    if (dist < RADIUS) {
      const pull = 1 - dist / RADIUS
      ref.current.style.transform = `translate(${(dx * STRENGTH * pull).toFixed(1)}px,${(dy * STRENGTH * pull).toFixed(1)}px)`
    } else if (ref.current.style.transform) {
      ref.current.style.transform = ''
    }
  }
  function onLeave() {
    if (ref.current) ref.current.style.transform = ''
  }
  function onDown() {
    if (ref.current) ref.current.classList.add('is-pressed')
  }
  function onUp() {
    if (ref.current) ref.current.classList.remove('is-pressed')
  }

  return (
    <Tag
      ref={ref}
      className={`magnetic-btn ${className}`}
      data-cursor="cta"
      onPointerMove={onMove}
      onPointerLeave={onLeave}
      onPointerDown={onDown}
      onPointerUp={onUp}
      {...props}
    >
      <span className="mb-inner">{children}</span>
    </Tag>
  )
}
