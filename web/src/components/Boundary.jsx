import './Boundary.css'

const LINES = [
  ['Watches', 'a fixed list of US equities on closed five-minute bars, premarket through after hours.'],
  ['Never', 'places an order, holds a key to your brokerage, or tells you what to buy.'],
  ['Journals', 'every observation before anyone sees it, including the ones below threshold.'],
  ['Publishes', 'its own record with the losing streaks left in.'],
]

export default function Boundary() {
  return (
    <section className="boundary" data-session-minutes="960" data-session-minutes-end="990">
      <p className="eyebrow"><b>16:00</b> the boundary</p>
      <h2 className="h2">We watch. You decide.</h2>
      <dl className="boundary-list">
        {LINES.map(([verb, rest]) => (
          <div key={verb} className="boundary-row">
            <dt>{verb}</dt>
            <dd>{rest}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
