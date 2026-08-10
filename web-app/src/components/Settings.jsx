import PerchMark from './PerchMark'
import './Views.css'
import './Settings.css'

// Telegram is the product's original, real delivery channel (see
// tradebot/telegram_bot/) -- it already works, just not from this
// dashboard yet. The bot's own username is resolved at runtime from
// the Telegram API (main.py's client.get_me()), never hardcoded, so
// there is no static handle to show here honestly. This section is
// informational only: no connect button, no invented bot link.
const TELEGRAM_STEPS = [
  { n: '01', label: 'Open Telegram' },
  { n: '02', label: 'Find the official Perch alerts bot' },
  { n: '03', label: 'Start the bot' },
  { n: '04', label: 'Connect your Perch account' },
  { n: '05', label: 'Choose which signals you want to receive' },
]

// The real, currently-existing feature set -- nothing here is gated
// today (see the plan/beta note above it), this is a preview of the
// architecture pricing will eventually hang off of.
const PRO_FEATURES = [
  'Market monitoring',
  'Perch signals',
  'Context around unusual activity',
  'Watchlists',
  'Signal history',
  'Dashboard access',
  'Future notification options',
]

function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s
}

export default function Settings({ account }) {
  const telegramLinked = account.linked_identities?.some((li) => li.provider === 'telegram')

  return (
    <div className="view">
      <span className="view-eyebrow"><span className="dot" /> SETTINGS</span>
      <h1>Your Perch account.</h1>
      <p className="view-subtitle">{account.email || 'Linked via Telegram'}</p>

      <div className="card settings-account-card">
        <div className="card-row">
          <span className="settings-label">Plan</span>
          <span className="settings-value">
            {capitalize(account.plan) || '—'}
            {account.founding_member && <span className="settings-badge">Founding member</span>}
          </span>
        </div>
        {account.plan === 'beta' && (
          <p className="settings-account-note">Full access while Perch is in beta — no card required.</p>
        )}
      </div>

      <section className="settings-section">
        <div className="settings-section-head">
          <h2>TELEGRAM ALERTS</h2>
          <p className="settings-section-sub">Take Perch with you.</p>
        </div>
        <p className="settings-body">
          Telegram alerts are another way to receive Perch signals outside the dashboard — a push
          to your phone the moment Perch notices something. It's an optional notification method,
          not a requirement: the dashboard is the primary way to use Perch.
        </p>

        {telegramLinked ? (
          <div className="settings-telegram-status">
            <PerchMark size={18} state="confirmed" />
            <div>
              <strong>Connected.</strong>
              <p>Your Telegram account is linked — alerts are delivered there as Perch detects them.</p>
            </div>
          </div>
        ) : (
          <>
            <ol className="settings-steps">
              {TELEGRAM_STEPS.map((s) => (
                <li key={s.n}>
                  <span className="settings-step-n">STEP {s.n}</span>
                  <span className="settings-step-label">{s.label}</span>
                </li>
              ))}
            </ol>
            <div className="settings-coming-soon">
              <span className="settings-coming-soon-badge"><span className="dot" /> COMING SOON</span>
              <p>Connecting Telegram from here isn't built yet. Telegram delivery will be available as an additional way to receive Perch signals.</p>
            </div>
          </>
        )}
      </section>

      <section className="settings-section">
        <div className="settings-section-head">
          <h2>PERCH PRO</h2>
          <p className="settings-section-sub">Unlock deeper signal monitoring and additional alert capabilities.</p>
        </div>
        <p className="settings-pro-note">Everything below is already included while Perch is in beta.</p>
        <ul className="settings-feature-list">
          {PRO_FEATURES.map((f) => (
            <li key={f}><span className="settings-check" aria-hidden="true">✓</span>{f}</li>
          ))}
        </ul>
        <div className="settings-pricing-note">Pricing coming soon.</div>
        <p className="settings-trust">
          Perch is an information and market-monitoring tool. It does not provide personalized investment advice.
        </p>
      </section>
    </div>
  )
}
