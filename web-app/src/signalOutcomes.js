// The fixed checkpoints journal.backfill_marks() resolves. This is a stable
// API presentation contract, not detection logic. Session close is separate
// because it is not a fixed offset.
const AFTER_DETECTION_OFFSETS = [15, 30, 60]

export function afterDetectionRows(marks) {
  return [
    ...AFTER_DETECTION_OFFSETS.map((offsetMin) => ({
      key: `offset-${offsetMin}`,
      label: `+${offsetMin} min after detection`,
      offsetMin,
      mark: marks?.find((mark) => mark.offset_min === offsetMin && !mark.at_close),
    })),
    {
      key: 'close',
      label: 'At session close',
      offsetMin: null,
      mark: marks?.find((mark) => mark.at_close),
    },
  ]
}

export function explicitOutcomeRows(data) {
  if (!Array.isArray(data?.outcomes)) return afterDetectionRows(data?.marks)
  return data.outcomes.map((outcome) => ({
    key: outcome.at_close ? 'close' : `offset-${outcome.offset_min}`,
    label: outcome.at_close ? 'At session close' : `+${outcome.offset_min} min after detection`,
    offsetMin: outcome.at_close ? null : outcome.offset_min,
    mark: outcome.price == null ? null : { price: outcome.price },
    status: outcome.status,
  }))
}

// The close batch resolves every checkpoint together rather than
// incrementally. Before the ledger supplies an explicit state, show a future
// target time only while it remains in the future; afterward say it is waiting
// for the close batch.
export function pendingResolutionLabel(offsetMin, tsUtc, nowMs = Date.now()) {
  if (offsetMin != null) {
    const targetMs = new Date(tsUtc).getTime() + offsetMin * 60 * 1000
    if (nowMs < targetMs) {
      const time = new Date(targetMs).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
      return { full: `Resolves ~${time}`, short: `Resolves ~${time}` }
    }
  }
  return { full: 'Resolves after session close', short: 'After close' }
}

export function outcomeResolutionLabel(status, offsetMin, tsUtc, nowMs = Date.now()) {
  if (!status || status === 'PENDING') return pendingResolutionLabel(offsetMin, tsUtc, nowMs)
  if (status === 'WAITING_FOR_CLOSE_BATCH') {
    return { full: 'Processing after session close', short: 'Processing' }
  }
  if (status === 'NOT_REACHED_BEFORE_CLOSE') {
    return { full: 'Not reached before session close', short: 'Not reached' }
  }
  if (status === 'DATA_UNAVAILABLE') {
    return { full: 'Outcome unavailable — data issue', short: 'Data unavailable' }
  }
  if (status === 'DELAYED') {
    return { full: 'Outcome delayed — check system status', short: 'Delayed' }
  }
  return { full: 'Outcome status unavailable', short: 'Unavailable' }
}
