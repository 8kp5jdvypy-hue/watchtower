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
// This is placeholder-grade geometry, not final brand artwork -- see
// BRAND.md for the plan to replace it with a professionally designed
// mark without touching every call site.
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
export const PERCH_MARK_VIEWBOX = '-72 -82 190 178'

const FILL = { ink: 'var(--ink)', cyan: 'var(--cyan)', dark: 'var(--bg)' }

// idle is the current, unchanged look. The other four exist so a future
// signal-aware placement (the alert experience, a loading state) can ask
// the mark to reflect what's happening without any caller needing to
// know how -- see the state-hook comment in PerchMark.css for exactly
// what each one does. Nothing in the app passes anything but the default
// today; this is architecture, not a new animation.
export const PERCH_MARK_STATES = ['idle', 'scanning', 'signal', 'confirmed', 'alert']

// The bare glyph -- just the four polygons + optional accent, no <svg>
// wrapper. Exists so call sites that need their own outer <svg> (a GSAP
// animation ref, a rotation transform for the mid-page dive) can still
// share the exact same polygon markup instead of hand-copying it, which
// is how the three implementations (this file, BootSequence, MarketField)
// drifted into independent copies in the first place. Standalone-icon
// usage should go through the default PerchMark export below, not this.
//
// fill defaults to 'currentColor' so most callers can just set CSS
// `color`. Pass fill={null} to omit the attribute entirely and let fill
// inherit from an ancestor instead (MarketField's dive kestrel sets
// fill/stroke on its own outer <svg> in CSS and relies on this).
export function PerchMarkGlyph({ fill = 'currentColor', accent = true }) {
  return (
    <g fill={fill ?? undefined}>
      <polygon opacity="0.82" points={FALCON_PATHS.farWing} />
      <polygon points={FALCON_PATHS.nearWing} />
      <polygon points={FALCON_PATHS.body} />
      <polygon points={FALCON_PATHS.tail} />
      {accent && <polyline className="pm-accent" points={FALCON_PATHS.accent} fill="none" />}
    </g>
  )
}

export default function PerchMark({ size = 26, className = '', variant = 'ink', accent = true, state = 'idle' }) {
  const fill = FILL[variant] || FILL.ink
  const safeState = PERCH_MARK_STATES.includes(state) ? state : 'idle'
  return (
    <svg
      className={`perch-mark pm-state-${safeState} ${className}`}
      width={size}
      height={size}
      viewBox={PERCH_MARK_VIEWBOX}
      data-state={safeState}
      aria-hidden="true"
    >
      <PerchMarkGlyph fill={fill} accent={accent && variant !== 'cyan'} />
    </svg>
  )
}
