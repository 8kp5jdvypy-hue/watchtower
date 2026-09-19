import { MARKET_STATE_LABEL } from '../lib/perchData'
import { SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './Hero.css'

export default function Hero({ status, record }) {
  const state = status?.market?.state
  const feed = status?.sources?.[0]
  const tracked = record?.track_record?.sample_size
  return (
    <section className="hero" id="top" data-session-minutes="420" data-session-minutes-end="560">
      <p className="eyebrow hero-status" aria-live="polite">
        <span className={`dot ${status ? '' : 'dot-off'}`} />
        {status ? (
          <>
            <b>{MARKET_STATE_LABEL[state] || state}</b>
            <span className="hero-sep">/</span>
            {feed?.status === 'operational' ? 'scanner live' : feed?.status === 'stale' ? 'scanner stale' : feed?.status || 'status unknown'}
            <span className="hero-sep">/</span>
            {new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(status.generatedAt))} ET
          </>
        ) : 'connecting to the scanner…'}
      </p>
      <h1 className="hero-title">
        The market moves.<br />
        <span className="hero-title-accent">Perch notices.</span>
      </h1>
      <p className="hero-lede">
        A scanner that watches a fixed list of US equities on five-minute bars, flags the moves that
        stand out from their own history, and explains the evidence in plain words. It never trades.
        {tracked ? <> It publishes its record — all {tracked} tracked calls, including the misses.</> : null}
      </p>
      <div className="hero-actions">
        <a className="btn btn-primary" href={withRef(SIGNUP_URL)} onClick={() => track('signup_cta_click', { source: 'hero' })}>Get access</a>
        <a className="btn btn-ghost" href="#session">Walk through today's session</a>
      </div>
      <p className="hero-fine">Free while Perch is in beta. An email link signs you in; no card, no password.</p>
    </section>
  )
}
