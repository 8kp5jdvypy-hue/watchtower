import { useState } from 'react'
import { api } from '../api'
import { SOURCE_LABELS, etDateTime, formatCentsParts, pnlToneClass } from '../journalFormat'
import JournalOverlay from './JournalOverlay'
import SignalDetail from './SignalDetail'
import './TradeDetail.css'

// Renders straight from the trade row the list already has -- the list
// endpoint returns full trade objects (see /journal/trades), so there's
// nothing extra to fetch and no skeleton state to design. Edit hands
// off to TradeSheet (via onEdit) rather than growing a second form here.
//
// A linked trade's click-through opens the existing SignalDetail overlay
// *stacked over* this one (both portal to document.body; SignalDetail
// mounts later, so it's the later body child and paints above -- no
// fixed-positioning fights with the panel's backdrop-filter/transform),
// rather than swapping views: closing the signal lands you back on the
// exact trade you left, which is the whole point of the link. While it's
// up, this overlay's own Escape/Tab handling is suspended -- see
// JournalOverlay's `suspended` prop.
export default function TradeDetail({ trade, onClose, onEdit, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)
  const [signalOpen, setSignalOpen] = useState(false)
  // 'idle' | 'checking' | 'unavailable' -- the click-through pre-checks
  // /signals/<id> before opening anything, because a stored snapshot can
  // outlive its detection row (the known users.db/journal.db atomicity
  // gap -- see _detection_snapshot in tradebot/api/app.py). A 404 marks
  // the element quietly unavailable instead of opening a broken overlay;
  // the snapshot's own facts stay right where they were.
  const [signalState, setSignalState] = useState('idle')
  const isSkip = trade.is_skip
  const snapshot = trade.detection_snapshot

  async function openSignal() {
    if (signalState !== 'idle') return
    setSignalState('checking')
    try {
      await api.signalDetail(trade.detection_id)
      setSignalState('idle')
      setSignalOpen(true)
    } catch {
      setSignalState('unavailable')
    }
  }

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
    <>
    <JournalOverlay
      label={`${trade.symbol} journal entry`} eyebrow="Journal entry" onClose={onClose}
      suspended={signalOpen}
    >
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
                <dd>{SOURCE_LABELS[trade.source] || trade.source}</dd>
              </div>
            )}
          </dl>

          {/* The linked signal -- the moment the journal and the alerts
              become one record. Three honest states: snapshot + live
              detail (full element, click-through), snapshot whose
              detail 404s at click time (facts stay, click retires
              quietly), and a bare detection_id with no snapshot (a
              muted one-line acknowledgment, not a broken card). */}
          {trade.detection_id != null && (
            snapshot ? (
              <button
                type="button"
                className={`td-signal${signalState === 'unavailable' ? ' is-unavailable' : ''}`}
                onClick={openSignal}
                disabled={signalState !== 'idle'}
              >
                <span className="td-signal-top">
                  <span className="td-signal-eyebrow">Perch signal</span>
                  {signalState === 'unavailable' ? (
                    <span className="td-signal-note">signal detail unavailable</span>
                  ) : (
                    <span className="td-signal-cue" aria-hidden="true">›</span>
                  )}
                </span>
                <span className="td-signal-headline">{snapshot.headlines}</span>
                <span className="td-signal-meta">
                  <span className={`td-signal-tier is-${snapshot.tier}`}>{snapshot.tier}</span>
                  <span className="td-signal-time">{etDateTime(snapshot.ts_utc)}</span>
                </span>
              </button>
            ) : (
              <p className="td-signal-fallback">Linked signal · detail unavailable</p>
            )
          )}

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
    {signalOpen && (
      <SignalDetail id={trade.detection_id} onClose={() => setSignalOpen(false)} />
    )}
    </>
  )
}
