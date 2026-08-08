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
          <a href="#demo" data-cursor="link">The interface</a>
          <a href="#waitlist" data-cursor="link">Waitlist</a>
        </nav>
      </div>
      <div className="wrap ft-base">
        <span>© {new Date().getFullYear()} Perch. Not investment advice. Perch does not place trades or control brokerage accounts.</span>
      </div>
    </footer>
  )
}
