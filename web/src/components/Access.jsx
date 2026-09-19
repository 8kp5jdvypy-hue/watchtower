import { LOGIN_URL, SIGNUP_URL } from '../config'
import { track, withRef } from '../analytics'
import './Access.css'

export default function Access() {
  return (
    <section className="access" id="access" data-session-minutes="1170" data-session-minutes-end="1200">
      <p className="eyebrow"><b>19:30</b> access</p>
      <h2 className="h2">Watch tomorrow's session with it running.</h2>
      <p className="lede">An email link signs you in; no password, no card. The dashboard is live now. The iPhone build follows by invitation.</p>
      <div className="access-actions">
        <a className="btn btn-primary" href={withRef(SIGNUP_URL)} onClick={() => track('signup_cta_click', { source: 'access' })}>Get access</a>
        <a className="btn btn-ghost" href={withRef(LOGIN_URL)} onClick={() => track('login_cta_click', { source: 'access' })}>Already in? Sign in</a>
      </div>
    </section>
  )
}
