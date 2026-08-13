import { useCallback, useMemo, useState } from 'react'
import { api, JOURNAL_EXPORT_URL } from '../api'
import { useApiData } from '../hooks/useApiData'
import {
  SOURCE_LABELS, etDateKey, etTime, formatCents, formatCentsCompact, formatCentsParts, pnlToneClass,
} from '../journalFormat'
import PerchMark from './PerchMark'
import TradeSheet from './TradeSheet'
import TradeDetail from './TradeDetail'
import './Views.css'
import './Journal.css'

// The Trade Journal tab. Top to bottom: today's P&L (the one hero
// number), the month calendar (ET days -- the API buckets, this only
// draws), the selected day's entries, then all-time stats that refuse
// to report a win rate off 3 trades. Summary/calendar/list all come
// from Phase 2 endpoints as-is; the client never re-buckets days.

const WEEKDAYS = ['S', 'M', 'T', 'W', 'T', 'F', 'S']

function monthLabel(month) {
  const [y, m] = month.split('-').map(Number)
  return new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function dayLabel(dateKey) {
  const [y, m, d] = dateKey.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('en-US', {
    weekday: 'long', month: 'short', day: 'numeric',
  })
}

// Leading blanks + one cell per day, for a Sun-first 7-column grid.
// Pure calendar arithmetic on y/m/d numbers -- no timezone involved,
// the ET-ness of a day comes entirely from the API's keys.
function monthCells(month) {
  const [y, m] = month.split('-').map(Number)
  const firstDow = new Date(y, m - 1, 1).getDay()
  const daysInMonth = new Date(y, m, 0).getDate()
  const cells = Array.from({ length: firstDow }, () => null)
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push(`${month}-${String(d).padStart(2, '0')}`)
  }
  return cells
}

export default function Journal() {
  const todayKey = etDateKey()
  const currentMonth = todayKey.slice(0, 7)
  const [month, setMonth] = useState(currentMonth)
  const [selectedDate, setSelectedDate] = useState(todayKey)
  const [refresh, setRefresh] = useState(0)
  const [sheet, setSheet] = useState(null) // {mode} | {trade} | null
  const [detailTrade, setDetailTrade] = useState(null)

  const fetchSummary = useCallback(() => api.journalSummary(), [])
  const summary = useApiData(fetchSummary, [refresh])
  const fetchCalendar = useCallback(() => api.journalCalendar(month), [month])
  const calendar = useApiData(fetchCalendar, [month, refresh])
  const fetchDay = useCallback(() => api.journalTrades(selectedDate), [selectedDate])
  const day = useApiData(fetchDay, [selectedDate, refresh])

  // Summary counts only priced trades (see db.py's _pnl_bucket) -- a
  // journal holding one skip would read as all zeros there. The empty
  // state must mean "never logged anything at all", so when the counts
  // are zero, ask the full-history endpoint before showing it.
  const countsAreZero = summary.data != null && summary.data.stats.total_trades === 0
  const fetchAll = useCallback(
    () => (countsAreZero ? api.journalTrades() : Promise.resolve(null)),
    [countsAreZero]
  )
  const allTrades = useApiData(fetchAll, [countsAreZero, refresh])
  const isEmpty = countsAreZero && allTrades.data != null && allTrades.data.trades.length === 0

  const cells = useMemo(() => monthCells(month), [month])

  function handleSaved(trade) {
    // Land the user on the day the entry belongs to (ET), so a
    // just-logged trade is visibly *there* the moment the sheet closes.
    const key = etDateKey(new Date(trade.taken_at))
    setSelectedDate(key)
    setMonth(key.slice(0, 7))
    setDetailTrade(null)
    setRefresh((r) => r + 1)
  }

  function handleDeleted() {
    setDetailTrade(null)
    setRefresh((r) => r + 1)
  }

  // Only the *first* load gets the bare loading/error screen -- a
  // refresh after logging a trade re-runs these fetches, and swapping
  // the whole view (with an overlay possibly mid-close) out for
  // "Loading…" would both flash the page and unmount the overlay's
  // close timer, leaving a re-opened sheet behind. useApiData keeps the
  // previous data during a refetch, so render through it.
  if (summary.loading && !summary.data) return <div className="view"><p className="empty-state">Loading…</p></div>
  if (summary.error && !summary.data) return <div className="view"><p className="empty-state">Couldn't load your journal.</p></div>
  if (!summary.data) return <div className="view"><p className="empty-state">Couldn't load your journal.</p></div>

  const s = summary.data.summary
  const stats = summary.data.stats

  const sheetEl = sheet && (
    <TradeSheet
      initialMode={sheet.mode}
      trade={sheet.trade ?? null}
      onClose={() => setSheet(null)}
      onSaved={handleSaved}
    />
  )

  // First-impression moment, not a fallback: the whole tab becomes the
  // invitation until the first entry exists.
  if (isEmpty) {
    return (
      <div className="view">
        <span className="view-eyebrow"><span className="dot" /> TRADE JOURNAL</span>
        <div className="quiet-state jr-empty">
          <PerchMark size={30} state="idle" />
          <h2>Your journal starts here.</h2>
          <p>Log the trades you take — and the setups you pass on — in seconds. Perch keeps the honest record of how you actually trade.</p>
          <button type="button" className="jr-add" onClick={() => setSheet({ mode: 'trade' })}>
            Log a trade
          </button>
        </div>
        {sheetEl}
      </div>
    )
  }

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> TRADE JOURNAL</span>
      <div className="jr-head">
        <div>
          <h1>The honest record.</h1>
          <p className="view-subtitle">Takes and passes, logged in seconds, kept in your words.</p>
        </div>
        <div className="jr-actions">
          <button type="button" className="jr-add" onClick={() => setSheet({ mode: 'trade' })}>
            Log a trade
          </button>
          <button type="button" className="jr-add-skip" onClick={() => setSheet({ mode: 'skip' })}>
            Log a pass
          </button>
        </div>
      </div>

      <div className="jr-summary">
        <div className="stat-tile jr-hero">
          <div className="stat-tile-label">Today</div>
          <div className={`jr-hero-value ${pnlToneClass(s.today.trade_count ? s.today.pnl_cents : null)}`}>
            {s.today.trade_count
              ? (() => {
                  const parts = formatCentsParts(s.today.pnl_cents, { sign: true })
                  return <><span className="pnl-mark">{parts.prefix}</span>{parts.value}</>
                })()
              : '—'}
          </div>
          <div className="jr-tile-sub">
            {s.today.trade_count
              ? `${s.today.trade_count} trade${s.today.trade_count === 1 ? '' : 's'} · ${s.today.wins}W ${s.today.losses}L`
              : 'Nothing logged yet today'}
          </div>
        </div>
        {[['This week', s.week], ['This month', s.month], ['All time', s.all_time]].map(([label, p]) => (
          <div className="stat-tile jr-tile" key={label}>
            <div className="stat-tile-label">{label}</div>
            <div className={`jr-tile-value ${pnlToneClass(p.trade_count ? p.pnl_cents : null)}`}>
              {p.trade_count ? formatCents(p.pnl_cents, { sign: true }) : '—'}
            </div>
            <div className="jr-tile-sub">{p.trade_count} trade{p.trade_count === 1 ? '' : 's'}</div>
          </div>
        ))}
      </div>

      <div className="jr-cal card">
        <div className="jr-cal-head">
          <button
            type="button" className="jr-cal-nav" aria-label="Previous month"
            onClick={() => setMonth((m) => shiftMonth(m, -1))}
          >
            ‹
          </button>
          <span className="jr-cal-title">{monthLabel(month)}</span>
          <button
            type="button" className="jr-cal-nav" aria-label="Next month"
            disabled={month >= currentMonth}
            onClick={() => setMonth((m) => shiftMonth(m, 1))}
          >
            ›
          </button>
        </div>
        <div className="jr-cal-grid" role="grid" aria-label={`${monthLabel(month)} trading calendar, Eastern-time days`}>
          {WEEKDAYS.map((w, i) => (
            <span className="jr-cal-weekday" key={`${w}-${i}`} aria-hidden="true">{w}</span>
          ))}
          {cells.map((key, i) => {
            if (!key) return <span className="jr-cal-blank" key={`blank-${i}`} />
            const info = calendar.data?.days?.[key]
            const future = key > todayKey
            const tone = info
              ? (info.pnl_cents > 0 ? 'is-up' : info.pnl_cents < 0 ? 'is-down' : 'is-flat')
              : ''
            return (
              <button
                key={key}
                type="button"
                className={[
                  'jr-cal-day', tone,
                  key === selectedDate ? 'is-selected' : '',
                  key === todayKey ? 'is-today' : '',
                ].filter(Boolean).join(' ')}
                disabled={future}
                aria-pressed={key === selectedDate}
                aria-label={`${dayLabel(key)}${info ? `, ${formatCents(info.pnl_cents, { sign: true })}, ${info.trade_count || 'no priced'} trades` : ', no entries'}`}
                onClick={() => setSelectedDate(key)}
              >
                <span className="jr-cal-num">{Number(key.slice(8))}</span>
                {info && (
                  <span className="jr-cal-cell-data">
                    {/* trade_count is priced trades only -- a skip-only or
                        unpriced day still earns its quiet dot. */}
                    <span className="jr-cal-pnl">{info.trade_count ? formatCentsCompact(info.pnl_cents) : '·'}</span>
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      <section className="jr-day">
        <h2 className="jr-section-title">
          {dayLabel(selectedDate)}
          {day.data && day.data.trades.length > 0 && (
            <span className="jr-section-count"> · {day.data.trades.length} entr{day.data.trades.length === 1 ? 'y' : 'ies'}</span>
          )}
        </h2>
        {day.loading && !day.data && <p className="empty-state">Loading…</p>}
        {day.error && !day.data && <p className="empty-state">Couldn't load this day.</p>}
        {day.data && day.data.trades.length === 0 && (
          <p className="jr-day-empty">Nothing logged this day.</p>
        )}
        {day.data && day.data.trades.map((trade) => (
          <button type="button" className={`jr-row${trade.is_skip ? ' jr-row-skip' : ''}`} key={trade.id} onClick={() => setDetailTrade(trade)}>
            <span className="jr-row-main">
              <span className="jr-row-top">
                <span className="jr-row-symbol">{trade.symbol}</span>
                {trade.direction && <span className="jr-chip">{trade.direction}</span>}
                {trade.is_skip && <span className="jr-chip jr-chip-skip">passed</span>}
                {trade.source && <span className="jr-chip jr-chip-source">{SOURCE_LABELS[trade.source] || trade.source}</span>}
              </span>
              {(trade.is_skip ? trade.skip_reason : trade.note) && (
                <span className="jr-row-note">{trade.is_skip ? trade.skip_reason : trade.note}</span>
              )}
            </span>
            <span className="jr-row-right">
              <span className={`jr-row-pnl ${trade.is_skip ? 'pnl-none' : pnlToneClass(trade.pnl_cents)}`}>
                {trade.is_skip ? '—' : formatCents(trade.pnl_cents, { sign: true })}
              </span>
              <span className="jr-row-time">{etTime(trade.taken_at)}</span>
            </span>
          </button>
        ))}
      </section>

      <section className="jr-stats">
        <h2 className="jr-section-title">Your numbers</h2>
        {stats.meaningful ? (
          <div className="stat-grid jr-stat-grid">
            <div className="stat-tile">
              <div className="stat-tile-label">Priced trades</div>
              <div className="stat-tile-value">{stats.total_trades}</div>
              <div className="headline">{stats.winning_trades} wins · {stats.losing_trades} losses</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Win rate</div>
              <div className="stat-tile-value">{Math.round(stats.win_rate * 100)}%</div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Avg win</div>
              <div className={`stat-tile-value ${stats.avg_win_cents != null ? 'pnl-up' : ''}`}>
                {formatCents(stats.avg_win_cents, { sign: true })}
              </div>
            </div>
            <div className="stat-tile">
              <div className="stat-tile-label">Avg loss</div>
              <div className={`stat-tile-value ${stats.avg_loss_cents != null ? 'pnl-down' : ''}`}>
                {formatCents(stats.avg_loss_cents)}
              </div>
            </div>
          </div>
        ) : (
          // The honest small-sample state -- same discipline as
          // Performance's "not enough history" and SignalDetail's
          // small-sample pill: no win rate computed from 3 data points.
          <div className="jr-stats-quiet">
            <p>
              <b>{stats.total_trades}</b> priced trade{stats.total_trades === 1 ? '' : 's'} logged — win rate and
              averages appear at 5. Perch doesn't report numbers it can't stand behind yet.
            </p>
          </div>
        )}
        {/* Consistency of the habit, deliberately never a P&L target --
            counting entries journaled, the only "progress" this page
            tracks. */}
        {s.month.trade_count > 0 && (
          <p className="jr-consistency">
            {s.month.trade_count} trade{s.month.trade_count === 1 ? '' : 's'} journaled in {monthLabel(currentMonth).split(' ')[0]}.
          </p>
        )}
        <a className="jr-export" href={JOURNAL_EXPORT_URL}>
          Export CSV — it's your data
        </a>
      </section>

      {sheetEl}
      {detailTrade && (
        <TradeDetail
          trade={detailTrade}
          onClose={() => setDetailTrade(null)}
          onEdit={(trade) => { setDetailTrade(null); setSheet({ trade }) }}
          onDeleted={handleDeleted}
        />
      )}
    </div>
  )
}
