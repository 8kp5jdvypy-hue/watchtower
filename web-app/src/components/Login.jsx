import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { track } from '../analytics'
import PerchMark from './PerchMark'
import AmbientField from './AmbientField'
import './Login.css'

// There is exactly one real mechanism here: email in, magic link out.
// /auth/magic-link/request and /verify treat a new address and an
// existing one identically on purpose (see tradebot/api/app.py) -- so
// "signup" and "login" are the same request to the same endpoint, with
// two different framings layered on top for the two different moments
// a visitor arrives in. Neither mode ever learns which case they were.
const COPY = {
  signup: {
    eyebrow: 'CREATE YOUR PERCH ACCOUNT',
    body: "Enter your email and we'll send you a secure link to enter Perch.",
    switchPrompt: 'Already have an account?',
    switchAction: 'Log in',
    switchTo: 'login',
  },
  login: {
    eyebrow: 'WELCOME BACK',
    body: "Enter your email and we'll send you a secure link to return to Perch.",
    switchPrompt: "Don't have an account?",
    switchAction: 'Sign up',
    switchTo: 'signup',
  },
}

function getInitialMode() {
  const params = new URLSearchParams(window.location.search)
  return params.get('mode') === 'login' ? 'login' : 'signup'
}

export default function Login() {
  const [mode, setMode] = useState(getInitialMode)
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState('idle') // idle | sending | sent | error
  const [fieldError, setFieldError] = useState('')
  const inputRef = useRef(null)
  const copy = COPY[mode]

  // Reflect the mode in the URL without a hard navigation, so the toggle
  // link and browser back/forward both do something sensible, and a
  // reload keeps whichever framing the visitor was looking at.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    params.set('mode', mode)
    window.history.replaceState(null, '', `${window.location.pathname}?${params}`)
  }, [mode])

  function switchMode(next) {
    setMode(next)
    setStatus('idle')
    setFieldError('')
  }

  async function submit(event) {
    event.preventDefault()
    const trimmed = email.trim()
    if (!trimmed || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      setFieldError('Enter a valid email address.')
      inputRef.current?.focus()
      return
    }
    setFieldError('')
    setStatus('sending')
    try {
      await api.requestMagicLink(trimmed)
      setStatus('sent')
      track('magic_link_sent', { mode })
    } catch {
      setStatus('error')
    }
  }

  return (
    <div className="login-shell">
      <AmbientField />
      <div className="login-card">
        <PerchMark size={30} className="login-mark" state={status === 'sending' ? 'scanning' : status === 'sent' ? 'confirmed' : 'idle'} />

        {status === 'sent' ? (
          <div className="login-sent">
            <span className="login-sent-eyebrow"><span className="dot" /> CHECK YOUR EMAIL</span>
            <p className="login-sent-body">We've sent a secure sign-in link to <b>{email}</b>.</p>
            <p className="login-sent-sub">It expires in 15 minutes. No password to remember — just follow the link.</p>
            <button type="button" className="login-different-email" onClick={() => { setStatus('idle'); setEmail('') }}>
              Use a different email
            </button>
          </div>
        ) : (
          <>
            <h1>{copy.eyebrow}</h1>
            <p>{copy.body}</p>
            <form className="login-form" onSubmit={submit} noValidate>
              <input
                ref={inputRef}
                type="email"
                inputMode="email"
                autoComplete="email"
                required
                placeholder="you@example.com"
                value={email}
                onChange={(e) => { setEmail(e.target.value); if (fieldError) setFieldError('') }}
                disabled={status === 'sending'}
                aria-invalid={!!fieldError}
                aria-describedby={fieldError ? 'login-field-error' : undefined}
              />
              <button type="submit" disabled={status === 'sending'}>
                {status === 'sending' ? 'Sending…' : 'Send magic link'}
              </button>
              {fieldError && <p className="login-error" id="login-field-error">{fieldError}</p>}
              {status === 'error' && <p className="login-error">Something went wrong — try again.</p>}
            </form>

            <p className="login-passwordless">
              <span className="dot" /> No passwords. Just your email, every time.
            </p>

            <p className="login-switch">
              {copy.switchPrompt}{' '}
              <button type="button" onClick={() => switchMode(copy.switchTo)}>{copy.switchAction}</button>
            </p>
          </>
        )}
      </div>
    </div>
  )
}
