import { useState } from 'react'
import { api } from '../api'

export default function Login() {
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState('idle') // idle | sending | sent | error

  const submit = async (event) => {
    event.preventDefault()
    setStatus('sending')
    try {
      await api.requestMagicLink(email)
      setStatus('sent')
    } catch {
      setStatus('error')
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <h1>Perch</h1>
        <p>The market moves. Perch notices.</p>
        {status === 'sent' ? (
          <p className="login-success">
            Check {email} for a sign-in link. It expires in 15 minutes.
          </p>
        ) : (
          <form className="login-form" onSubmit={submit}>
            <input
              type="email"
              required
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={status === 'sending'}
            />
            <button type="submit" disabled={status === 'sending'}>
              {status === 'sending' ? 'Sending…' : 'Send sign-in link'}
            </button>
            {status === 'error' && <p className="login-error">Something went wrong — try again.</p>}
          </form>
        )}
      </div>
    </div>
  )
}
