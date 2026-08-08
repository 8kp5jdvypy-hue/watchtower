import { useInViewOnce } from '../hooks/useInViewOnce'
import './SignalGlyph.css'

// The one recurring Perch motif: scattered noise resolving into a single
// clear signal. This is the "signature visual" -- it appears wherever the
// site needs a quiet punctuation mark (loading, section breaks, footer),
// never as decoration for its own sake, and it's static enough to cost
// nothing at rest.
const NOISE = [
  [6, 8], [14, 23], [21, 5], [29, 19], [37, 9], [45, 25],
  [52, 6], [59, 17], [66, 27], [72, 10],
]

export default function SignalGlyph({ className = '' }) {
  const [ref, inView] = useInViewOnce(0.5)
  return (
    <svg
      ref={ref}
      className={`signal-glyph${inView ? ' is-in' : ''}${className ? ` ${className}` : ''}`}
      viewBox="0 0 120 32"
      aria-hidden="true"
    >
      {NOISE.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r="1.3" className="sg-noise" style={{ transitionDelay: `${i * 0.03}s` }} />
      ))}
      <line x1="84" y1="16" x2="97" y2="16" className="sg-line" />
      <circle cx="107" cy="16" r="2.6" className="sg-signal" />
    </svg>
  )
}
