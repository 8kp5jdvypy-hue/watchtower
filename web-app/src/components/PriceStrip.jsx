import { useCallback } from 'react'
import { api } from '../api'
import { usePolling } from '../hooks/usePolling'
import './PriceStrip.css'

/*
 * The price feed's instruments as one quiet strip: crypto then the
 * scanner's equities, last price, and an honest freshness mark. A
 * stale value (every source failed; a cached one is being served) is
 * shown with a hollow dot and dimmed; a missing one prints an em dash.
 * The same feed the Telegram /price command and the iOS watchlist read.
 */
const ORDER = ['BTC', 'ETH', 'SOL', 'SPY', 'QQQ', 'GOOGL', 'TSLA', 'BE', 'IONQ']
const PRICE_POLL_MS = 60_000

function fmt(value) {
  if (value == null) return '\u2014'
  return value >= 1000 ? value.toLocaleString('en-US', { maximumFractionDigits: 0 }) : value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export default function PriceStrip() {
  const fetchPrices = useCallback(() => api.prices(), [])
  const { data, error } = usePolling(fetchPrices, { intervalMs: PRICE_POLL_MS })
  const prices = data?.prices ?? {}
  const symbols = ORDER.filter((s) => s in prices || !data)
  return (
    <div className="price-strip" aria-label="Last prices">
      {symbols.map((symbol) => {
        const p = prices[symbol]
        return (
          <span key={symbol} className={`price-cell ${p?.stale ? 'is-stale' : ''} ${p ? '' : 'is-missing'}`} title={p ? `${p.source} · ${new Date(p.ts_utc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}${p.stale ? ' · stale' : ''}` : 'unavailable'}>
            <span className="price-dot" aria-hidden="true" />
            <b>{symbol}</b>
            <span className="price-value">{p ? fmt(p.price) : '\u2014'}</span>
          </span>
        )
      })}
      {error && !data && <span className="price-cell is-missing"><b>prices</b><span className="price-value">unavailable</span></span>}
    </div>
  )
}
