import './Record.css'

/*
 * Numbers as proof — Perch's, including the ones that hurt. Every value
 * is read from /public/track-record; labels say exactly what each is.
 * If the record is unreachable the section renders its empty state.
 */
function pctf(v, digits = 1) { return `${(v * 100).toFixed(digits)}%` }

export default function Record({ record }) {
  const tr = record?.track_record
  const stats = tr ? [
    ['Hit rate', pctf(tr.hit_rate), `of ${tr.sample_size} tracked HIGH alerts continued in their direction at ${tr.offset_min} minutes`],
    ['Longest losing streak', String(tr.longest_losing_streak), 'consecutive misses, left in the record'],
    ['Average move', `${tr.avg_return_pct >= 0 ? '+' : ''}${tr.avg_return_pct.toFixed(2)}%`, `signed, at ${tr.offset_min} minutes, across the same sample`],
    ['Statistical edge', tr.significance?.is_significant ? 'yes' : 'not yet', tr.significance ? `z = ${tr.significance.z_score.toFixed(2)}; about ${tr.significance.n_needed_for_meaningful_edge.toLocaleString()} tracked calls would be needed to say either way` : ''],
  ] : []
  return (
    <section className="record" id="record" data-session-minutes="990" data-session-minutes-end="1050">
      <p className="eyebrow"><b>16:30</b> the record</p>
      <h2 className="h2">What the journal says about Perch.</h2>
      <p className="lede">Most tools show you their wins. This is the whole ledger, computed from the journal by the same code that computes it for paying users, refreshed as sessions close.</p>
      {tr ? (
        <dl className="record-grid">
          {stats.map(([label, value, note]) => (
            <div key={label} className="record-stat">
              <dt>{label}</dt>
              <dd><b>{value}</b><span>{note}</span></dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="record-empty">The public record is unreachable right now. Perch does not show cached or invented numbers in its place.</p>
      )}
      <p className="record-foot">
        {tr ? <>{tr.total_alerts} HIGH alerts sent to date; {tr.total_no_trade} carried a no-trade flag and are excluded from the sample above. </> : null}
        Full ledger: <a href="/record.html">perchmarkets.com/record</a>.
      </p>
    </section>
  )
}
