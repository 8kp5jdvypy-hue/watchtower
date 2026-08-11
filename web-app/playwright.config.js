import { defineConfig, devices } from '@playwright/test'

// Deliberately narrow scope: this covers the one flow that's been
// hand-verified with one-off CDP scripts across many separate design
// passes this project has gone through (logged-out -> Login, magic-
// link request, authenticated session bypassing Login, logout) --
// exactly the kind of thing a redesign six months from now could
// silently break without anyone noticing until a real user hits it.
// Not a general component-test suite; see e2e/README.md.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5183',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'npm run dev -- --port 5183',
    url: 'http://localhost:5183',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
