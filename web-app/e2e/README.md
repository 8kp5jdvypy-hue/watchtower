# e2e

Playwright coverage for one specific thing: the auth flow (Login.jsx +
App.jsx's `account === null` gating). Not a general component-test
suite -- this exists because that flow has been hand-verified with
one-off CDP scripts across several separate design passes on this
project, and a scripted, throwaway check doesn't catch a regression
introduced by the *next* pass.

No real backend involved. `fixtures.js` intercepts every API call via
Playwright's `page.route()` and answers with the same response shapes
`tradebot/api/app.py` actually returns (see `../src/api.js` for the
real endpoint list) -- so these tests catch a frontend wiring mistake
(a route that stopped being called, a state that never resets) without
needing Flask, sqlite, or a network connection.

Run: `npm run test:e2e` (starts its own `vite dev` on :5183 via
`playwright.config.js`'s `webServer`, same port the rest of this
project's manual CDP scripts have used all along -- if one of those is
already running on :5183, Playwright reuses it instead of starting a
second one).
