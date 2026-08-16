// Same suite on port 5187 instead of 5183. Exists because
// reuseExistingServer will happily reuse a dev server another
// worktree/session left running on 5183 -- and then the suite silently
// tests THAT tree's code (this actually happened: two "failures" on
// 2026-08-15 were a five-day-old server from a stale worktree). Use
// this config whenever a foreign server might hold the default port:
//   npx playwright test --config playwright.port.config.js
import base from './playwright.config.js'

export default {
  ...base,
  use: { ...base.use, baseURL: 'http://localhost:5187' },
  webServer: {
    ...base.webServer,
    command: 'npm run dev -- --port 5187',
    url: 'http://localhost:5187',
  },
}
