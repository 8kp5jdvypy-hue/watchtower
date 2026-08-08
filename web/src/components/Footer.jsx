import PerchMark from './PerchMark'
import './Footer.css'
import './PerchMark.css'

export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="wrap ft-inner">
        <div className="ft-brand">
          <a href="#top" className="ft-mark-row" data-cursor="link">
            <PerchMark size={20} />
            <span>PERCH</span>
          </a>
          <p>Market intelligence for everyone.</p>
        </div>
        <nav className="ft-nav">
          <a href="#field" data-cursor="link">What it watches</a>
          <a href="#coverage" data-cursor="link">Coverage</a>
          <a href="#demo" data-cursor="link">The interface</a>
          <a href="#waitlist" data-cursor="link">Waitlist</a>
        </nav>
      </div>
      <div className="wrap ft-base">
        <p className="ft-disclaimer">
          Perch surfaces information. It does not predict market movement, provide financial advice,
          or guarantee any result. It never places trades and never has access to your brokerage account.
          Nothing here is investment advice — decisions, and their consequences, are yours.
        </p>
        <span>© {new Date().getFullYear()} Perch.</span>
      </div>
    </footer>
  )
}
