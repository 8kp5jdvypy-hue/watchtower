import './PerchMark.css'

// The Perch falcon mark: a single reusable component, every usage across
// the site (nav, footer, hero texture source, mid-page dive moment, boot
// sequence, favicon) draws from this same polygon data so the identity is
// actually consistent. Deliberately geometric rather than illustrative:
// straight-edged facets reading as an aerodynamic, diving falcon rather
// than a literal bird illustration -- sleek and abstract, and inherently
// wings-spread, which is what makes it work for both a still hero mark
// and an actual diving motion at the mid-page moment.
//
// variant: 'ink' (default, near-white -- for dark backgrounds) | 'cyan' |
// 'dark' (near-black -- for light backgrounds)
// accent: show the single thin cyan leading-edge line (default true).
// Silhouette is intentionally ~90% of the mark; the accent is the other 10%.
export const FALCON_PATHS = {
  farWing: '-8,-38 -62,-72 -18,-18',
  nearWing: '10,-22 108,-46 30,26 4,-4',
  body: '46,-64 14,-16 -32,58 -2,-30',
  tail: '-32,58 -58,86 -20,72',
  accent: '10,-22 108,-46',
}

const FILL = { ink: 'var(--ink)', cyan: 'var(--cyan)', dark: 'var(--bg)' }

export default function PerchMark({ size = 26, className = '', variant = 'ink', accent = true }) {
  const fill = FILL[variant] || FILL.ink
  return (
    <svg
      className={`perch-mark ${className}`}
      width={size}
      height={size}
      viewBox="-72 -82 190 178"
      aria-hidden="true"
    >
      <g fill={fill}>
        <polygon opacity="0.82" points={FALCON_PATHS.farWing} />
        <polygon points={FALCON_PATHS.nearWing} />
        <polygon points={FALCON_PATHS.body} />
        <polygon points={FALCON_PATHS.tail} />
        {accent && variant !== 'cyan' && (
          <polyline className="pm-accent" points={FALCON_PATHS.accent} fill="none" />
        )}
      </g>
    </svg>
  )
}
