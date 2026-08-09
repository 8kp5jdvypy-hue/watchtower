import './PerchMark.css'

// The Perch kestrel mark: a line-art raptor perched on a rising
// stock-chart line, looking down in patient observation. Every usage
// across the site (nav, footer, hero texture source, mid-page dive
// moment, boot sequence, favicon) draws from this same path data, so
// the identity is actually one mark, not several independent drawings.
export const KESTREL = {
  skull: { cx: 178, cy: 50, r: 30 },
  beak: 'M203,38 L228,64 Q236,72 226,76 L198,60 Z',
  eye: { cx: 192, cy: 40, r: 4.5 },
  nostril: { cx: 218, cy: 54, r: 1.5 },
  body: 'M150,75 C125,82 108,100 102,125 C97,148 100,172 112,188 C122,200 136,205 150,203 C166,200 178,188 182,168 C186,145 182,120 170,100 C162,86 156,78 150,75 Z',
  wing: 'M152,90 C132,100 118,120 116,145 C114,168 122,186 138,195 C146,198 152,195 154,190 C144,180 138,166 138,150 C138,132 146,115 158,102 C156,96 154,92 152,90 Z',
  feathers: [
    'M144,108 C133,125 128,145 130,165',
    'M152,112 C142,130 138,150 139,168',
    'M159,120 C151,138 147,156 147,174',
  ],
  tail: [
    'M118,188 L104,240', 'M128,195 L120,248', 'M138,200 L138,252', 'M148,200 L156,250', 'M156,195 L168,244',
  ],
  legs: ['M125,193 L123,222', 'M148,198 L150,222'],
  talons: ['M117,222 L123,222 L128,216', 'M144,222 L150,222 L156,215'],
  perch: 'M45,222 L162,222 L185,202 L210,210 L235,178 L262,150',
}

// viewBox bounds -- tight around the full composition (bird + perch +
// rising chart line), used everywhere so the mark is always shown whole.
const VB = { x: 30, y: 0, w: 260, h: 260 }

const COLOR = { cyan: 'var(--cyan)', ink: 'var(--ink)', dark: 'var(--bg)' }

export default function PerchMark({ size = 26, className = '', variant = 'cyan', bg = false }) {
  const color = COLOR[variant] || COLOR.cyan
  return (
    <svg
      className={`perch-mark ${className}`}
      width={size}
      height={size * (VB.h / VB.w)}
      viewBox={`${VB.x} ${VB.y} ${VB.w} ${VB.h}`}
      aria-hidden="true"
    >
      {bg && <rect x={VB.x} y={VB.y} width={VB.w} height={VB.h} fill="var(--bg)" />}
      <g className="pm-glow" fill="none" stroke={color}>
        <path d={KESTREL.perch} />
        {KESTREL.tail.map((d, i) => <path key={i} d={d} />)}
        {KESTREL.legs.map((d, i) => <path key={i} d={d} />)}
        {KESTREL.talons.map((d, i) => <path key={i} d={d} />)}
        <path className="pm-fill" d={KESTREL.body} />
        <path d={KESTREL.wing} />
        {KESTREL.feathers.map((d, i) => <path key={i} d={d} />)}
        <circle className="pm-fill" cx={KESTREL.skull.cx} cy={KESTREL.skull.cy} r={KESTREL.skull.r} />
        <path className="pm-fill" d={KESTREL.beak} />
      </g>
      <circle className="pm-eye" cx={KESTREL.eye.cx} cy={KESTREL.eye.cy} r={KESTREL.eye.r} fill={color} />
      <circle className="pm-eye" cx={KESTREL.nostril.cx} cy={KESTREL.nostril.cy} r={KESTREL.nostril.r} fill={color} />
    </svg>
  )
}
