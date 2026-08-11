import { test, expect } from '@playwright/test'
import { mockLoggedOut, mockLoggedIn, defaultAccount } from './fixtures.js'

test.describe('unauthenticated visitor', () => {
  test('sees the Sign Up screen by default', async ({ page }) => {
    await mockLoggedOut(page)
    await page.goto('/')
    await expect(page.locator('h1')).toHaveText('CREATE YOUR PERCH ACCOUNT')
    await expect(page.locator('.login-passwordless')).toContainText('No passwords')
  })

  test('?mode=login shows the Log In framing instead', async ({ page }) => {
    await mockLoggedOut(page)
    await page.goto('/?mode=login')
    await expect(page.locator('h1')).toHaveText('WELCOME BACK')
  })

  test('the Log in / Sign up toggle switches modes without a reload', async ({ page }) => {
    await mockLoggedOut(page)
    await page.goto('/')
    await page.getByRole('button', { name: 'Log in' }).click()
    await expect(page.locator('h1')).toHaveText('WELCOME BACK')
    await expect(page).toHaveURL(/mode=login/)
  })

  test('rejects an invalid email without ever calling the API', async ({ page }) => {
    await mockLoggedOut(page)
    let requestMade = false
    await page.route('**/auth/magic-link/request', (route) => {
      requestMade = true
      route.fulfill({ status: 202, json: { ok: true } })
    })
    await page.goto('/')
    await page.getByPlaceholder('you@example.com').fill('not-an-email')
    await page.getByRole('button', { name: 'Send magic link' }).click()
    await expect(page.locator('.login-error')).toContainText('valid email')
    expect(requestMade).toBe(false)
  })

  test('a valid email sends the magic link and shows the reassuring "check your email" state', async ({ page }) => {
    await mockLoggedOut(page)
    await page.goto('/')
    await page.getByPlaceholder('you@example.com').fill('visitor@example.com')
    await page.getByRole('button', { name: 'Send magic link' }).click()
    await expect(page.getByText('CHECK YOUR EMAIL')).toBeVisible()
    await expect(page.locator('.login-sent-body')).toContainText('visitor@example.com')
  })

  test('"Use a different email" returns to a clean, empty form', async ({ page }) => {
    await mockLoggedOut(page)
    await page.goto('/')
    await page.getByPlaceholder('you@example.com').fill('visitor@example.com')
    await page.getByRole('button', { name: 'Send magic link' }).click()
    await expect(page.getByText('CHECK YOUR EMAIL')).toBeVisible()

    await page.getByRole('button', { name: 'Use a different email' }).click()
    await expect(page.getByPlaceholder('you@example.com')).toHaveValue('')
    await expect(page.locator('h1')).toBeVisible()
  })
})

test.describe('authenticated session', () => {
  test('bypasses Login entirely, even when the URL still says ?mode=signup', async ({ page }) => {
    await mockLoggedIn(page)
    await page.goto('/?mode=signup')
    await expect(page.locator('.login-shell')).toHaveCount(0)
    await expect(page.locator('.app-shell')).toBeVisible()
  })

  test('persists across a reload without flashing Login', async ({ page }) => {
    await mockLoggedIn(page)
    await page.goto('/')
    await expect(page.locator('.app-shell')).toBeVisible()
    await page.reload()
    await expect(page.locator('.app-shell')).toBeVisible()
    await expect(page.locator('.login-shell')).toHaveCount(0)
  })

  test('shows the signed-in account email in the topbar', async ({ page }) => {
    await mockLoggedIn(page, { ...defaultAccount(), email: 'someone@example.com' })
    await page.goto('/')
    await expect(page.locator('.topbar-account')).toContainText('someone@example.com')
  })

  test('Sign out returns cleanly to the Login screen', async ({ page }) => {
    await mockLoggedIn(page)
    await page.goto('/')
    await expect(page.locator('.app-shell')).toBeVisible()

    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.locator('h1')).toHaveText('CREATE YOUR PERCH ACCOUNT')
    await expect(page.locator('.app-shell')).toHaveCount(0)
  })
})
