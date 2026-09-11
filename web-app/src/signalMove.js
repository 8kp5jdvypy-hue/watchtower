export function signalMove(signal, quote) {
  const finiteNumber = (value) => {
    if (value == null || value === '') return null
    const number = Number(value)
    return Number.isFinite(number) ? number : null
  }

  const detectionPrice = finiteNumber(signal?.close)
  const livePrice = finiteNumber(quote?.last)
  if (Number.isFinite(livePrice) && Number.isFinite(detectionPrice) && detectionPrice > 0) {
    return {
      valuePct: ((livePrice - detectionPrice) / detectionPrice) * 100,
      label: 'since alert',
      source: 'live_quote',
    }
  }

  const recordedMove = finiteNumber(signal?.session_move_at_alert_pct)
  if (
    signal?.session_move_at_alert_status === 'AVAILABLE'
    && Number.isFinite(recordedMove)
  ) {
    return {
      valuePct: recordedMove,
      label: 'at alert',
      source: 'recorded_detection',
    }
  }

  return null
}
