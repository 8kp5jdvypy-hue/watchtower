import { etParts, outcomeOf } from '../lib/perchData'
import './Anatomy.css'

/*
 * "How it reads": one real alert from the public record, taken apart.
 * The headline Perch actually sent, its parts labelled, and what the
 * record says happened 30 minutes later. Picks the most recent alert
 * that has an outcome; if the record is unreachable it says so rather
 * than showing an invented example.
 */
const PARTS = [
  ['symbol', 'the name', 'One of a fixed watchlist. Perch does not roam.'],
  ['range', 'this bar’s range', 'High minus low of one closed 5-minute bar, in dollars.'],
  ['multiple', 'in ATR units', 'The range divided by the name’s own 14-day average true range. Every threshold in Perch is an ATR multiple, never a percentage.'],
]

function parseHeadline(h) {
  // "MSFT bar range 3.11 is 5.3x ATR(14)=0.59"
  const m = /^(\S+) bar range ([\d.]+) is ([\d.]+)x ATR\(14\)=([\d.]+)$/.exec(h || '')
  if (!m) return null
  return { symbol: m[1], range: m[2], multiple: m[3], atr: m[4] }
}

export default function Anatomy({ record }) {
  const alerts = record?.alerts || []
  const pick = alerts.filter((a) => a.return_pct != null && parseHeadline(a.headline)).sort((a, b) => b.sent_at.localeCompare(a.sent_at))[0]
  const parsed = pick ? parseHeadline(pick.headline) : null
  const t = pick ? etParts(pick.sent_at) : null
  const outcome = pick ? outcomeOf(pick) : null
  return (
    <section className="anatomy" id="reads" data-session-minutes="570" data-session-minutes-end="600">
      <p className="eyebrow"><b>09:30</b> how it reads</p>
      <h2 className="h2">One bar. Its own history. A sentence.</h2>
      <p className="lede">Perch does not predict. It notices when a closed five-minute bar is out of proportion to what that name usually does, and says so in one line you can check.</p>
      {pick ? (
        <figure className="anatomy-fig">
          <figcaption className="anatomy-cap">
            <span>Sent {t.date}, {t.time} ET</span>
            <span>detection {pick.detection_id.slice(0, 8)}</span>
          </figcaption>
          <div className="anatomy-headline" aria-label={pick.headline}>
            <span className="tok tok-symbol">{parsed.symbol}</span>
            <span className="tok-plain"> bar range </span>
            <span className="tok tok-range">{parsed.range}</span>
            <span className="tok-plain"> is </span>
            <span className="tok tok-multiple">{parsed.multiple}×</span>
            <span className="tok-plain"> ATR(14)={parsed.atr}</span>
          </div>
          <dl className="anatomy-parts">
            {PARTS.map(([k, name, why]) => (
              <div key={k} className={`anatomy-part part-${k}`}>
                <dt>{name}</dt>
                <dd>{why}</dd>
              </div>
            ))}
          </dl>
          <p className={`anatomy-outcome outcome-${outcome.kind}`}>
            <span className="eyebrow">what happened next</span>
            <b>{outcome.label}</b>
            <span>Recorded by the journal, not written by us. {outcome.kind === 'reversed' ? 'This one went the other way. They’re in the record too.' : 'Direction held for the checkpoint window.'}</span>
          </p>
        </figure>
      ) : (
        <p className="anatomy-empty">The public record is unreachable right now, so there is no example to show. Perch does not fabricate one.</p>
      )}
    </section>
  )
}
