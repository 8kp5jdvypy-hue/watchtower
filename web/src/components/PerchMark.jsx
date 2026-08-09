import './PerchMark.css'

// The Perch kestrel mark -- the client's own reference artwork (a line-art
// kestrel perched on a rising stock-chart line), used as-is rather than
// hand-redrawn. Every usage across the site (nav, footer, hero texture
// source, mid-page dive moment, boot sequence, favicon) points at the
// same asset: /public/perch-kestrel.png.
export const KESTREL_MARK_SRC = '/perch-kestrel.png'
export const KESTREL_MARK_ASPECT = 499 / 467

export default function PerchMark({ size = 26, className = '' }) {
  return (
    <img
      className={`perch-mark ${className}`}
      src={KESTREL_MARK_SRC}
      width={size * KESTREL_MARK_ASPECT}
      height={size}
      alt=""
      aria-hidden="true"
    />
  )
}
