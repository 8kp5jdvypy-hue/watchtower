import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ApiError,
  ApiProtocolError,
  ApiTimeoutError,
  parseApiResponse,
  request,
} from '../src/api.js'
import { authFailureState } from '../src/authSession.js'

function response({ ok, status, body, parseError = null }) {
  return {
    ok,
    status,
    json: async () => {
      if (parseError) throw parseError
      return body
    },
  }
}

test('successful malformed or null JSON is an explicit protocol error', async () => {
  await assert.rejects(
    parseApiResponse(response({ ok: true, status: 200, parseError: new SyntaxError('bad json') })),
    ApiProtocolError,
  )
  await assert.rejects(
    parseApiResponse(response({ ok: true, status: 200, body: null })),
    ApiProtocolError,
  )
})

test('non-success responses retain API status even with an unreadable body', async () => {
  await assert.rejects(
    parseApiResponse(response({ ok: false, status: 502, parseError: new SyntaxError('proxy html') })),
    (error) => error instanceof ApiError && error.status === 502,
  )
})

test('only a real 401 becomes signed out', () => {
  assert.equal(authFailureState(new ApiError(401, { error: 'not authenticated' })), 'signed-out')
  assert.equal(authFailureState(new ApiError(500, { error: 'server failure' })), 'unavailable')
  assert.equal(authFailureState(new ApiProtocolError(200)), 'unavailable')
  assert.equal(authFailureState(new TypeError('network failed')), 'unavailable')
})

test('hung requests become bounded timeout errors', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')))
  })
  try {
    await assert.rejects(request('/test-timeout', { timeoutMs: 5 }), ApiTimeoutError)
  } finally {
    globalThis.fetch = originalFetch
  }
})
