import './PerchMark.css'

/*
 * The Perch mark — "P on the rail" (web/DESIGN-DIRECTION-2026-09.md).
 *
 * A bold geometric P whose stem is the observation rail: faint ruled
 * ticks up its left edge, and one cyan flag under the bowl — the thing
 * Perch noticed. At 16px the ticks vanish and the P + flag still holds;
 * at 512px the ticks read as a field instrument's scale. The flag is the
 * same gesture as the live-status dot and the "closer look" marker in
 * the product, so mark and product share one device.
 *
 * Geometry lives here once. web-app/src/components/PerchMark.jsx is a
 * byte-identical copy (keep them in sync), and the iOS app carries the
 * same paths in perch-mobile-mvp/src/components/PerchMark.tsx.
 *
 * States (PERCH_MARK_STATES) are CSS hooks on the flag and ticks — see
 * PerchMark.css.
 */

export const PERCH_MARK_VIEWBOX = '0 0 100 100'

// Stem (the rail) and bowl of the P as one even-odd path; the flag; the
// rail's ruled ticks.
export const MARK_PATHS = {
  letter:
    'M27 10 H52 A22 22 0 0 1 52 54 H38 V89 A3 3 0 0 1 35 92 H27 A3 3 0 0 1 24 89 V13 A3 3 0 0 1 27 10 Z ' +
    'M38 22 V42 H50 A10 10 0 0 0 50 22 Z',
  flag: 'M38 62 H62 A4 4 0 0 1 62 70 H38 Z',
  ticks: [22, 34, 46, 58, 70, 82],
}

const FILL = { ink: 'var(--ink)', cyan: 'var(--cyan)', dark: 'var(--bg)' }

export const PERCH_MARK_STATES = ['idle', 'scanning', 'signal', 'confirmed', 'alert']

/** Bare glyph for callers that own their <svg> (animation refs, transforms). */
export function PerchMarkGlyph({ fill = 'currentColor', accent = true, ticks = true }) {
  return (
    <g>
      {ticks && (
        <g className="pm-ticks" stroke={fill ?? undefined} strokeWidth="2" strokeLinecap="round" opacity="0.38">
          {MARK_PATHS.ticks.map((y) => (
            <line key={y} x1="14" y1={y} x2="20" y2={y} />
          ))}
        </g>
      )}
      <path className="pm-letter" d={MARK_PATHS.letter} fill={fill ?? undefined} fillRule="evenodd" />
      <path className="pm-flag" d={MARK_PATHS.flag} fill={accent ? 'var(--cyan)' : fill ?? undefined} />
    </g>
  )
}

/**
 * Standalone icon. `variant` picks the letter colour; the flag is cyan
 * unless `accent` is false (the one-colour edition: app icon, print).
 */
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
      <PerchMarkGlyph fill={fill} accent={accent && variant !== 'cyan'} ticks={size >= 40} />
    </svg>
  )
}

/** Mark + wordmark. Wordmark is Space Grotesk 600, +0.18em — the app header. */
export function PerchLockup({ size = 22, className = '', state = 'idle' }) {
  return (
    <span className={`perch-lockup ${className}`}>
      <PerchMark size={size} state={state} />
      <span className="perch-wordmark">Perch</span>
    </span>
  )
}
