import { test, expect } from '@playwright/test'
import { mockLoggedIn, defaultAccount } from './fixtures.js'

// A malformed API response is a more realistic trigger than injected
// script: `account.email` rendered directly as a React child throws
// "Objects are not valid as a React child" if the API ever sent
// something that isn't a string there -- exactly the kind of thing
// ErrorBoundary.jsx exists to catch instead of the app just going blank.
test('a render crash shows the recovery screen instead of a blank page', async ({ page }) => {
  await mockLoggedIn(page, { ...defaultAccount(), email: { not: 'a string' } })

  const beacons = []
  await page.route('**/client-errors', (route) => {
    beacons.push(route.request().postData())
    route.fulfill({ status: 204, body: '' })
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Something went wrong.' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Reload' })).toBeVisible()

  expect(beacons.length).toBeGreaterThan(0)
  expect(beacons[0]).toContain('not valid as a React child')
})
