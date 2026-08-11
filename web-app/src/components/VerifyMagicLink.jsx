import { useState } from 'react'
import { api } from '../api'
import PerchMark from './PerchMark'
import AmbientField from './AmbientField'
import './Login.css'

// A bare GET here (the old design) could authenticate a browser as
// whoever the token belongs to just by being LOADED -- an <img
// src="…/auth/magic-link/verify?token=…"> on any page, no click, no JS,
// would fire the request and the Set-Cookie would land regardless. This
// screen exists so the actual sign-in is a same-origin, JSON POST
// (api.verifyMagicLink) that only app.perchmarkets.com's own fetch can
// trigger -- request()'s Content-Type: application/json forces a CORS
// preflight, and tradebot/api/app.py's allowed_origins check rejects
// anything else before the real request ever reaches the server. A
// passive page load (email preview, link-scanning security appliance,
// an <img> tag) can only ever render this button, never press it.
export default function VerifyMagicLink({ token, onVerified }) {
  const [status, setStatus] = useState('idle') // idle | verifying | error

  async function confirm() {
    setStatus('verifying')
    try {
      await api.verifyMagicLink(token)
      onVerified()
    } catch {
      setStatus('error')
    }
  }

  return (
    <div className="login-shell">
      <AmbientField />
      <div className="login-card">
        <PerchMark size={30} className="login-mark" state={status === 'verifying' ? 'scanning' : 'idle'} />
        <h1>CONFIRM SIGN-IN</h1>
        <p>Tap below to finish signing into Perch on this device.</p>
        <form className="login-form" onSubmit={(e) => { e.preventDefault(); confirm() }} noValidate>
          <button type="submit" disabled={status === 'verifying'}>
            {status === 'verifying' ? 'Signing in…' : 'Confirm sign-in'}
          </button>
          {status === 'error' && (
            <p className="login-error">That link is invalid or has expired — request a new one.</p>
          )}
        </form>
      </div>
    </div>
  )
}
