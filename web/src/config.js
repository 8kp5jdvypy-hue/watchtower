// Single source for the landing site's links into the real product --
// every "Sign up" / "Log in" CTA on this site should import from here
// rather than hardcoding app.perchmarkets.com, so there's one place to
// change if that domain or the mode-query convention ever moves.
export const SIGNUP_URL = 'https://app.perchmarkets.com/?mode=signup'
export const LOGIN_URL = 'https://app.perchmarkets.com/?mode=login'
