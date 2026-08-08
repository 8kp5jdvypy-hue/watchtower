import PerchMark from './PerchMark'
import MagneticButton from './MagneticButton'
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
        <p>Request early access to Perch.</p>
        <MagneticButton as="a" href="#waitlist">Join the waitlist</MagneticButton>
      </div>
    </section>
  )
}
