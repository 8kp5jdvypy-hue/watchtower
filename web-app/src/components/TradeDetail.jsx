import { useState } from 'react'
import { api } from '../api'
import { SOURCE_LABELS, etDateTime, formatCentsParts, pnlToneClass } from '../journalFormat'
import JournalOverlay from './JournalOverlay'
import './TradeDetail.css'

// Renders straight from the trade row the list already has -- the list
// endpoint returns full trade objects (see /journal/trades), so there's
// nothing extra to fetch and no skeleton state to design. Edit hands
// off to TradeSheet (via onEdit) rather than growing a second form here.
export default function TradeDetail({ trade, onClose, onEdit, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)
  const isSkip = trade.is_skip

  async function remove(requestClose) {
    if (deleting) return
    setError(null)
    setDeleting(true)
    try {
      await api.journalDeleteTrade(trade.id)
      onDeleted(trade)
      requestClose()
    } catch (err) {
      setError(err.body?.error || 'Couldn’t delete this entry. Try again.')
      setDeleting(false)
      setConfirming(false)
    }
  }

  return (
    <JournalOverlay label={`${trade.symbol} journal entry`} eyebrow="Journal entry" onClose={onClose}>
      {(requestClose) => (
        <div className="td-body">
          <div className="td-title-row">
            <span className="td-symbol">{trade.symbol}</span>
            <span className="td-chips">
              {trade.direction && <span className={`td-chip td-chip-${trade.direction}`}>{trade.direction}</span>}
              {isSkip && <span className="td-chip td-chip-skip">passed</span>}
            </span>
          </div>

          {isSkip ? (
            <p className="td-no-position">No position taken &mdash; logged as a pass.</p>
          ) : (
            <div className={`td-pnl ${pnlToneClass(trade.pnl_cents)}`}>
              {(() => {
                const parts = formatCentsParts(trade.pnl_cents, { sign: true })
                if (!parts) return '—'
                return <span><span className="pnl-mark">{parts.prefix}</span>{parts.value}</span>
              })()}
              {trade.pnl_cents == null && <span className="td-pnl-unpriced">no P&amp;L recorded</span>}
            </div>
          )}

          <dl className="td-meta">
            <div className="td-meta-row">
              <dt>When</dt>
              <dd>{etDateTime(trade.taken_at)}</dd>
            </div>
            {trade.source && (
              <div className="td-meta-row">
                <dt>Source</dt>
                {/* Static provenance chip only in Phase 3 -- linking a
                    perch_signal entry back to its signal is Phase 4. */}
                <dd>{SOURCE_LABELS[trade.source] || trade.source}</dd>
              </div>
            )}
          </dl>

          {isSkip && trade.skip_reason && (
            <div className="td-section">
              <h2 className="td-section-title">Why you passed</h2>
              <p className="td-text">{trade.skip_reason}</p>
            </div>
          )}
          {!isSkip && trade.note && (
            <div className="td-section">
              <h2 className="td-section-title">Note</h2>
              <p className="td-text">{trade.note}</p>
            </div>
          )}

          {error && <p className="td-error" role="alert">{error}</p>}

          <div className="td-foot">
            {confirming ? (
              <div className="td-confirm">
                <span>Delete this entry for good?</span>
                <button type="button" className="td-btn td-btn-danger" onClick={() => remove(requestClose)} disabled={deleting}>
                  {deleting ? 'Deleting…' : 'Delete'}
                </button>
                <button type="button" className="td-btn" onClick={() => setConfirming(false)} disabled={deleting}>
                  Keep it
                </button>
              </div>
            ) : (
              <>
                <button type="button" className="td-btn td-btn-quiet" onClick={() => setConfirming(true)}>
                  Delete
                </button>
                <button type="button" className="td-btn td-btn-primary" onClick={() => onEdit(trade)}>
                  Edit
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </JournalOverlay>
  )
}
