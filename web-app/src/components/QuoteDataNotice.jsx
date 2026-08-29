import { formatEtTime } from '../hooks/useLiveStatus'
import { quoteStatusMessage } from '../quoteFreshness'

export default function QuoteDataNotice({ status, lastSuccessAt }) {
  const message = quoteStatusMessage(status)
  if (!message) return null
  const updated = formatEtTime(lastSuccessAt)
  return (
    <p
      className={`data-trust-notice data-trust-notice-${status}`}
      role={status === 'unavailable' ? 'alert' : 'status'}
    >
      {message}{updated ? ` Last successful update: ${updated} ET.` : ''}
    </p>
  )
}
