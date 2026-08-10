import PerchMark from './PerchMark'
import MagneticButton from './MagneticButton'
import { SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './FinalCta.css'
import './PerchMark.css'

export default function FinalCta() {
  return (
    <section className="final-cta">
      <div className="fc-glow" aria-hidden="true" />
      <div className="wrap fc-inner">
        <PerchMark size={22} className="fc-mark" />
        <h2>
          BE THERE<br />WHEN IT MOVES.
        </h2>
        <p>Create your Perch account — no card required.</p>
        <MagneticButton as="a" href={withRef(SIGNUP_URL)} onClick={() => track('signup_cta_click', { source: 'final_cta' })}>Sign up</MagneticButton>
      </div>
    </section>
  )
}
