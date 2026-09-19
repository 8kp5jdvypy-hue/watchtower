import { PerchLockup } from './PerchMark'
import { MARKET_STATE_LABEL } from '../lib/perchData'
import './Footer.css'

export default function Footer({ status }) {
  const state = status?.market?.state
  return (
    <footer className="footer" data-session-minutes="1200">
      <div className="footer-row">
        <PerchLockup size={20} />
        <span className="footer-status">
          <span className={`dot ${status ? '' : 'dot-off'}`} />
          {status ? `${MARKET_STATE_LABEL[state] || state} · scanner ${status.sources?.[0]?.status || 'unknown'}` : 'status unavailable'}
        </span>
      </div>
      <div className="footer-row footer-links">
        <a href="/record.html">Record</a>
        <a href="https://app.perchmarkets.com/">Dashboard</a>
        <a href="/privacy">Privacy</a>
        <a href="/terms">Terms</a>
        <a href="mailto:hello@perchmarkets.com">hello@perchmarkets.com</a>
      </div>
      <p className="footer-fine">Perch Markets. Market data by Alpaca (IEX). Research only — nothing here is a recommendation to buy or sell any security. Perch never places orders and has no brokerage access.</p>
    </footer>
  )
}
