import { ApiError } from './api.js'

// Only an authoritative 401 means the browser has no valid session. Network,
// protocol, and server failures are availability problems; converting them
// into "signed out" would falsely tell an authenticated user their identity
// changed and invite unnecessary magic-link churn.
export function authFailureState(error) {
  return error instanceof ApiError && error.status === 401
    ? 'signed-out'
    : 'unavailable'
}
