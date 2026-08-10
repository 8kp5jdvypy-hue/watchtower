import MagneticButton from './MagneticButton'
import { SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './Pricing.css'

// Same real feature list as the dashboard's own Settings/plan section
// (web-app/src/components/Settings.jsx) -- one honest list of what
// exists today, kept in sync by hand since the two are separate apps.
// No numbers here because none are final yet; see FREE_FEATURES below
// for why "beta" reads as a real, current state rather than a stand-in
// for a price.
const FEATURES = [
  'Market monitoring',
  'Perch signals',
  'Context around unusual activity',
  'Watchlists',
  'Signal history',
  'Dashboard access',
  'Future notification options',
]

export default function Pricing() {
  return (
    <section className="pricing" id="pricing">
      <div className="wrap pr-inner">
        <span className="eyebrow"><span className="dot" /> PERCH / PRICING</span>
        <h2>Free while Perch is in beta.</h2>
        <p className="pr-sub">No card required. Early users get full access — pricing for what comes after launches later.</p>

        <div className="pr-card">
          <div className="pr-card-head">
            <span className="pr-plan-name">PERCH</span>
            <span className="pr-plan-price">Beta <b>· Free</b></span>
          </div>
          <ul className="pr-features">
            {FEATURES.map((f) => (
              <li key={f}><span aria-hidden="true">✓</span>{f}</li>
            ))}
          </ul>
          <MagneticButton as="a" href={withRef(SIGNUP_URL)} className="pr-cta" onClick={() => track('signup_cta_click', { source: 'pricing' })}>Sign up</MagneticButton>
        </div>

        <p className="pr-trust">
          Perch is an information and market-monitoring tool. It does not provide personalized investment advice.
        </p>
      </div>
    </section>
  )
}
