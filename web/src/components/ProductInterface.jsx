import { SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './ProductInterface.css'

// The one real-product-proof section on the page (Tier 2). Unlike every
// other section here, this is NOT a demo -- it's a real screenshot of the
// actual shipped dashboard (web-app/src/components/SignalCard.jsx),
// captured by running the real, unmodified detector pipeline over cached
// historical market data (not hand-written example data). The specific
// signals shown are from a past session, not a live account -- captioned
// honestly below, the mirror image of every "Demo" tag elsewhere on this
// page.
//
// To re-capture after a future card/detail redesign:
//   1. From tradebot/, run the real detectors over cached sessions (see
//      scripts/replay.py for the pattern -- a wrapper that also sets
//      primary_kind on write, which that script doesn't) into a
//      throwaway journal.db.
//   2. Create a magic-link token directly via tradebot.accounts against a
//      throwaway users.db for a clearly-fake demo account -- no email
//      round-trip needed.
//   3. Run the API (tradebot.api.app.create_app) and the dashboard dev
//      server against those throwaway DBs.
//   4. Log in (inject the session cookie or hit the verify URL), open the
//      Signals tab, screenshot.
//   5. Crop to a well-varied, mostly-MEDIUM window (avoid the closing-bell
//      minute, where range_expansion fires correlated across symbols),
//      redact the account email in the topbar, save as
//      public/brand/product-interface.webp.
export default function ProductInterface() {
  return (
    <section className="product-interface" id="interface">
      <div className="wrap">
        <div className="section-head">
          <span className="eyebrow"><span className="dot" /> THE DASHBOARD</span>
          <h2>Not a mockup. This is what's running.</h2>
          <p className="pi-sub">
            The real card anatomy, from the real product — kind, tier, plain-English headline,
            the technical read underneath it.
          </p>
        </div>

        <div className="pi-frame">
          <img
            src="/brand/product-interface.webp"
            alt="The Perch dashboard Signals tab, showing three real signal cards: a HIGH-tier USO relative-strength signal with level-break and VWAP-break tags, an AMZN range expansion, and an SPY range expansion, each with a tier badge and a plain-English headline carrying the signal's own numbers"
            loading="lazy"
            width="1280"
            height="815"
          />
        </div>
        <p className="pi-caption">
          The actual Perch dashboard — signals from a recent session, not a live account.
        </p>

        <p className="pi-close">
          This is the dashboard every signal lands in.{' '}
          <a href={withRef(SIGNUP_URL)} data-cursor="link" onClick={() => track('signup_cta_click', { source: 'product_interface' })}>
            Sign up →
          </a>
        </p>
      </div>
    </section>
  )
}
