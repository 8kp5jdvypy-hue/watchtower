export const QUOTE_POLL_INTERVAL_MS = 10_000
export const QUOTE_STALE_AFTER_MS = QUOTE_POLL_INTERVAL_MS * 3

function stringArray(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : []
}

export function normalizeQuoteResponse(body, requestedSymbols = []) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new TypeError('invalid quote response body')
  }
  if (!body.quotes || typeof body.quotes !== 'object' || Array.isArray(body.quotes)) {
    throw new TypeError('quote response is missing quotes')
  }

  const quotes = {}
  for (const [symbol, quote] of Object.entries(body.quotes)) {
    if (!quote || typeof quote !== 'object' || !Number.isFinite(quote.last)) {
      throw new TypeError(`invalid quote for ${symbol}`)
    }
    quotes[symbol] = quote
  }

  const freshness = body.freshness && typeof body.freshness === 'object'
    ? body.freshness
    : {}
  const inferredMissing = requestedSymbols.filter((symbol) => !(symbol in quotes))
  return {
    quotes,
    freshness: {
      providerError: freshness.provider_error === true,
      staleSymbols: stringArray(freshness.stale_symbols),
      missingSymbols: Array.isArray(freshness.missing_symbols)
        ? stringArray(freshness.missing_symbols)
        : inferredMissing,
      checkedAtUtc: typeof freshness.checked_at_utc === 'string' ? freshness.checked_at_utc : null,
    },
  }
}

export function classifyQuoteFreshness({
  requestedCount,
  quoteCount,
  loading,
  error,
  lastSuccessAt,
  freshness,
  now = Date.now(),
  staleAfterMs = QUOTE_STALE_AFTER_MS,
}) {
  if (requestedCount === 0) return 'idle'
  const hasSuccess = lastSuccessAt != null
  if (loading && !hasSuccess) return 'loading'
  if (error && !hasSuccess) return 'unavailable'
  if (error && hasSuccess) return 'reconnecting'
  if (freshness?.providerError && quoteCount === 0) return 'unavailable'
  if (freshness?.providerError || freshness?.staleSymbols?.length > 0) return 'delayed'
  if (quoteCount === 0 || freshness?.missingSymbols?.length >= requestedCount) return 'unavailable'
  if (freshness?.missingSymbols?.length > 0 || quoteCount < requestedCount) return 'partial'
  if (hasSuccess && now - lastSuccessAt > staleAfterMs) return 'delayed'
  return hasSuccess ? 'live' : 'loading'
}

export function quoteStatusMessage(status) {
  if (status === 'unavailable') return 'Live prices are unavailable. Signal prices remain fixed at detection time.'
  if (status === 'reconnecting') return 'Live prices are reconnecting. Displayed live prices may be stale.'
  if (status === 'delayed') return 'Live prices are delayed. Displayed live prices may be stale.'
  if (status === 'partial') return 'Some live prices are unavailable. Missing prices are left blank.'
  return null
}
