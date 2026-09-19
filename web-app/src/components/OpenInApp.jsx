import PerchMark from './PerchMark'
import './OpenInApp.css'

/*
 * Browser landing for the iPhone app's universal links
 * (/auth/magic-link and /account/reauthenticate). On an iPhone with
 * the app installed, iOS opens the app directly via the AASA file and
 * this page is never shown. Anywhere else -- a Mac, a phone without
 * the app, an email client's preview -- this offers the app's custom
 * scheme with the same fragment. The token stays in the fragment
 * (never a query string, never sent to a server), exactly as the app's
 * authLinks parser expects.
 */
const ROUTES = {
  '/auth/magic-link': { scheme: 'perch://auth/magic-link', title: 'Finish signing in on your iPhone', body: 'This link signs you in to the Perch iPhone app. Open it on the phone where Perch is installed.' },
  '/account/reauthenticate': { scheme: 'perch://account/reauthenticate', title: 'Confirm it’s you on your iPhone', body: 'This link confirms an account action in the Perch iPhone app. Open it on the phone where Perch is installed.' },
}

export function isAppLinkPath(pathname) {
  return Object.prototype.hasOwnProperty.call(ROUTES, pathname)
}

export default function OpenInApp({ pathname, hash }) {
  const route = ROUTES[pathname]
  const fragment = hash && hash.startsWith('#') ? hash : ''
  const appUrl = `${route.scheme}${fragment}`
  const hasToken = /(^|[#&])token=[^&]{32,}/.test(fragment)
  return (
    <main className="open-in-app">
      <PerchMark size={28} state={hasToken ? 'signal' : 'idle'} />
      <h1>{route.title}</h1>
      <p>{route.body}</p>
      {hasToken ? (
        <a className="open-in-app-button" href={appUrl}>Open in the Perch app</a>
      ) : (
        <p className="open-in-app-warn">This link is missing its sign-in token. Request a fresh link from the app.</p>
      )}
      <p className="open-in-app-fine">Links expire after a few minutes and work once. Not on an iPhone? <a href="/">Use the web dashboard</a> instead.</p>
    </main>
  )
}
