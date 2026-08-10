import { useMemo } from 'react'
import './AmbientField.css'

// The "Perch is continuously watching" background presence the product
// brief asks for -- deliberately behind everything, low-opacity, and
// static positions with only a slow CSS-driven flicker (no JS-driven
// motion, no canvas). Cheap enough to leave mounted for the app's whole
// lifetime without a performance thought.
const SYMBOLS = ['SPY', 'QQQ', 'NVDA', 'AMD', 'AAPL', 'TSLA', 'MSFT', 'SOXX', 'XLK', 'DIA', 'IWM', 'META']

function useField() {
  return useMemo(() => {
    const rnd = (seed => () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed / 0x7fffffff
    })(13)
    return Array.from({ length: 22 }, (_, i) => ({
      sym: SYMBOLS[i % SYMBOLS.length],
      left: 4 + rnd() * 92,
      top: 6 + rnd() * 88,
      delay: rnd() * 6,
    }))
  }, [])
}

export default function AmbientField() {
  const items = useField()
  return (
    <div className="ambient-field" aria-hidden="true">
      {items.map((it, i) => (
        <span key={i} className="ambient-sym" style={{ left: `${it.left}%`, top: `${it.top}%`, animationDelay: `${it.delay}s` }}>
          {it.sym}
        </span>
      ))}
    </div>
  )
}
