import './TheApp.css'

const SCREENS = [
  { src: '/app/today.webp', alt: 'Perch iPhone app, Today tab: the research briefing with observations labelled closer look or context', label: 'Today' },
  { src: '/app/observation-detail.webp', alt: 'Perch iPhone app, observation detail: what happened, why it stands out, evidence and limitations', label: 'One observation' },
  { src: '/app/watchlist.webp', alt: 'Perch iPhone app, watchlist tab with per-symbol notification settings', label: 'Watchlist' },
]

export default function TheApp() {
  return (
    <section className="theapp" id="app" data-session-minutes="1080" data-session-minutes-end="1140">
      <p className="eyebrow"><b>18:00</b> the iPhone</p>
      <h2 className="h2">Same journal, in your pocket.</h2>
      <p className="lede">Screens from the iPhone build of 15 September — the demo session it ships with, labelled as such. Sign in with Apple or an email link; the watchlist and every observation come from the same record you see here.</p>
      <ul className="theapp-screens">
        {SCREENS.map((s) => (
          <li key={s.src}>
            <figure>
              <img src={s.src} alt={s.alt} width="700" height="1522" loading="lazy" decoding="async" />
              <figcaption>{s.label}</figcaption>
            </figure>
          </li>
        ))}
      </ul>
      <p className="theapp-note">Not on the App Store yet — TestFlight first. <a href="#access">Get access</a> and we will send the invitation when it opens.</p>
    </section>
  )
}
