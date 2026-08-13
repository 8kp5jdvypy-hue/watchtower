import { useState } from 'react'
import { api } from '../api'
import { SOURCE_LABELS, dollarsToCents, toApiIso } from '../journalFormat'
import JournalOverlay from './JournalOverlay'
import './TradeSheet.css'

// The fast-add form: one overlay, three jobs -- log a trade, log a
// skip ("saw the setup, passed" -- discipline worth recording, so it
// gets first-class treatment, not a checkbox), and edit an existing
// entry. Speed is the design constraint: symbol + one tap of anything
// else and submit. Everything except symbol is optional, exactly like
// the API (see tradebot/api/app.py's _parse_journal_payload).

const DIRECTIONS = [
  { value: 'long', label: 'Long' },
  { value: 'short', label: 'Short' },
]

function toLocalInputValue(date) {
  // datetime-local wants "YYYY-MM-DDTHH:mm" in the browser's own zone;
  // getTimezoneOffset is the clean way to get there without a library.
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

export default function TradeSheet({ initialMode = 'trade', trade = null, onClose, onSaved }) {
  const editing = trade != null
  // Editing never switches an entry between trade and skip -- the API
  // has no is_skip in its PATCH vocabulary, and "I actually took the
  // trade I said I skipped" is a delete + re-log, honestly.
  const [mode, setMode] = useState(editing ? (trade.is_skip ? 'skip' : 'trade') : initialMode)
  const isSkip = mode === 'skip'

  const [symbol, setSymbol] = useState(trade?.symbol ?? '')
  const [direction, setDirection] = useState(trade?.direction ?? null)
  const [pnl, setPnl] = useState(
    trade?.pnl_cents != null ? (trade.pnl_cents / 100).toFixed(2) : ''
  )
  const [takenAt, setTakenAt] = useState(() =>
    toLocalInputValue(trade ? new Date(trade.taken_at) : new Date())
  )
  const [source, setSource] = useState(trade?.source ?? null)
  const [note, setNote] = useState(trade?.note ?? '')
  const [skipReason, setSkipReason] = useState(trade?.skip_reason ?? '')
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  async function submit(e, requestClose) {
    e.preventDefault()
    if (saving) return
    setError(null)

    const cleanSymbol = symbol.trim().toUpperCase()
    if (!cleanSymbol) {
      setError('Symbol is required.')
      return
    }
    const when = takenAt ? new Date(takenAt) : new Date()
    if (Number.isNaN(when.getTime())) {
      setError('That date and time doesn’t parse.')
      return
    }

    const payload = { symbol: cleanSymbol, taken_at: toApiIso(when) }
    if (isSkip) {
      payload.skip_reason = skipReason.trim() || null
    } else {
      const cents = dollarsToCents(pnl)
      if (cents === undefined) {
        setError('P&L should be a dollar amount — like 125.50 or -40.')
        return
      }
      payload.direction = direction
      payload.source = source
      payload.pnl_cents = cents
      payload.note = note.trim() || null
    }

    setSaving(true)
    try {
      let saved
      if (editing) {
        saved = await api.journalUpdateTrade(trade.id, payload)
      } else {
        saved = await api.journalCreateTrade(isSkip ? { ...payload, is_skip: true } : payload)
      }
      onSaved(saved.trade)
      requestClose()
    } catch (err) {
      // 400 {error} from the API surfaces verbatim -- its messages are
      // already written for humans (see _parse_journal_payload).
      setError(err.body?.error || 'Couldn’t save this entry. Try again.')
      setSaving(false)
    }
  }

  const eyebrow = editing
    ? (isSkip ? 'Edit pass' : 'Edit trade')
    : (isSkip ? 'Log a pass' : 'Log a trade')

  return (
    <JournalOverlay label={eyebrow} eyebrow={eyebrow} onClose={onClose}>
      {(requestClose) => (
        <form className="ts-form" onSubmit={(e) => submit(e, requestClose)}>
          {!editing && (
            <div className="ts-mode" role="tablist" aria-label="Entry type">
              <button
                type="button" role="tab" aria-selected={!isSkip}
                className={`ts-mode-btn${!isSkip ? ' active' : ''}`}
                onClick={() => { setMode('trade'); setError(null) }}
              >
                Took the trade
              </button>
              <button
                type="button" role="tab" aria-selected={isSkip}
                className={`ts-mode-btn${isSkip ? ' active' : ''}`}
                onClick={() => { setMode('skip'); setError(null) }}
              >
                Passed on it
              </button>
            </div>
          )}

          {isSkip && !editing && (
            <p className="ts-skip-note">
              Passing on a setup is a decision worth keeping. No P&L here &mdash; just what you saw.
            </p>
          )}

          <div className="ts-field">
            <label className="ts-label" htmlFor="ts-symbol">Symbol</label>
            <input
              id="ts-symbol" className="ts-input ts-input-symbol" value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="NVDA" maxLength={12} autoComplete="off" autoCapitalize="characters"
              spellCheck={false} required
            />
          </div>

          {!isSkip && (
            <div className="ts-row">
              <div className="ts-field">
                <span className="ts-label" id="ts-direction-label">Direction</span>
                <div className="ts-seg" role="group" aria-labelledby="ts-direction-label">
                  {DIRECTIONS.map(({ value, label }) => (
                    <button
                      key={value} type="button" aria-pressed={direction === value}
                      className={`ts-seg-btn${direction === value ? ' active' : ''}`}
                      onClick={() => setDirection(direction === value ? null : value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="ts-field">
                <label className="ts-label" htmlFor="ts-pnl">P&amp;L, $</label>
                <input
                  id="ts-pnl" className="ts-input" value={pnl}
                  onChange={(e) => setPnl(e.target.value)}
                  placeholder="125.50 or -40" inputMode="decimal" autoComplete="off"
                />
              </div>
            </div>
          )}

          <div className="ts-field">
            <label className="ts-label" htmlFor="ts-taken-at">When</label>
            <input
              id="ts-taken-at" className="ts-input" type="datetime-local" value={takenAt}
              max={toLocalInputValue(new Date())}
              onChange={(e) => setTakenAt(e.target.value)}
            />
          </div>

          {!isSkip && (
            <div className="ts-field">
              <span className="ts-label" id="ts-source-label">How did you take this trade?</span>
              <div className="ts-seg ts-seg-wrap" role="group" aria-labelledby="ts-source-label">
                {Object.entries(SOURCE_LABELS).map(([value, label]) => (
                  <button
                    key={value} type="button" aria-pressed={source === value}
                    className={`ts-seg-btn${source === value ? ' active' : ''}`}
                    onClick={() => setSource(source === value ? null : value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {isSkip ? (
            <div className="ts-field">
              <label className="ts-label" htmlFor="ts-skip-reason">Why you passed</label>
              <textarea
                id="ts-skip-reason" className="ts-input ts-textarea" value={skipReason}
                onChange={(e) => setSkipReason(e.target.value)}
                placeholder="Spread too wide at the open&hellip;" rows={3} maxLength={2000}
              />
            </div>
          ) : (
            <div className="ts-field">
              <label className="ts-label" htmlFor="ts-note">Note <span className="ts-optional">optional</span></label>
              <textarea
                id="ts-note" className="ts-input ts-textarea" value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="What you saw, what you&rsquo;d repeat&hellip;" rows={3} maxLength={2000}
              />
            </div>
          )}

          {error && <p className="ts-error" role="alert">{error}</p>}

          <div className="ts-actions">
            <button type="button" className="ts-cancel" onClick={requestClose}>Cancel</button>
            <button type="submit" className="ts-submit" disabled={saving}>
              {saving ? 'Saving…' : editing ? 'Save changes' : isSkip ? 'Log pass' : 'Log trade'}
            </button>
          </div>
        </form>
      )}
    </JournalOverlay>
  )
}
