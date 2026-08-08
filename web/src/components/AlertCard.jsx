import './AlertCard.css'

export default function AlertCard({ symbol, kind = 'Unusual volume', detail, time, visible }) {
  return (
    <div className={`alert-card${visible ? ' is-visible' : ''}`} data-cursor="data">
      <div className="ac-head">
        <span className="eyebrow"><span className="dot" /> PERCH DETECTED</span>
        <span className="demo-tag">Demo</span>
      </div>
      <div className="ac-body">
        <span className="ac-symbol">{symbol}</span>
        <span className="ac-kind">{kind}</span>
        <p className="ac-detail">{detail}</p>
      </div>
      <div className="ac-foot">
        <span className="ac-time">{time}</span>
        <button className="ac-view" data-cursor="link">View signal</button>
      </div>
    </div>
  )
}
