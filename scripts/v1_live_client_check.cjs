// Live end-to-end check of /v1 with the iOS app's REAL compiled client.
// Prereqs: (1) in perch-mobile-mvp run `npm run test:api` (builds .test-build);
// (2) a local Flask on 127.0.0.1:8765 with a seeded journal -- see the
// docs/telegram-sunset-plan-2026-09.md M1 gate. Usage:
//   node scripts/v1_live_client_check.cjs <dir containing users.db>
// PERCH_MOBILE_APP overrides the app checkout path.
// The app's REAL compiled client (perch-mobile-mvp/.test-build) driven
// against the local Flask /v1. Every response passes the client's own
// zod decode or the call throws a 'contract' PerchApiError.
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const APP = process.env.PERCH_MOBILE_APP ?? path.join(process.env.HOME, 'Documents/Codex/2026-09-01/c/outputs/perch-mobile-mvp');
const { createPerchClient, PerchApiError } = require(path.join(APP, '.test-build/api/perchClient.js'));
const { createMemorySessionStore } = require(path.join(APP, '.test-build/auth/session.js'));
const { magicLinkTokenFromUrl } = require(path.join(APP, '.test-build/auth/authLinks.js'));
const DB = process.argv[2];

// The client insists on https; this harness's transport maps it to the local http port.
const fetchImpl = (url, init) => fetch(String(url).replace('https://localhost:8765', 'http://127.0.0.1:8765'), init);
const store = createMemorySessionStore();
const client = createPerchClient('https://localhost:8765', store, { fetchImpl });
const step = (name, ok, extra = '') => console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${extra ? '  ' + extra : ''}`);

(async () => {
  let failures = 0;
  const run = async (name, fn) => { try { const r = await fn(); step(name, true, r); } catch (e) { failures++; step(name, false, `${e.constructor.name} ${e.kind ?? ''} ${e.code ?? ''} ${e.message}`); } };

  await run('getStatus (public)', async () => { const s = await client.getStatus(); return `${s.status} market=${s.market.state}`; });
  await run('getMethodology (public)', async () => { const m = await client.getMethodology(); return `version=${m.version}`; });
  await run('unauthenticated getToday -> session_required', async () => {
    try { await client.getToday(); throw new Error('expected auth error'); } catch (e) { if (e instanceof PerchApiError && e.code === 'session_required') return e.code; throw e; } });
  await run('requestMagicLink', async () => { const r = await client.requestMagicLink('ios-live@example.com'); return `accepted=${r.accepted}`; });
  // Pull the token exactly the way the device would: from the emailed universal link (fragment).
  const token = execFileSync('sqlite3', [path.join(DB, 'users.db'), "SELECT token FROM magic_link_tokens WHERE email='ios-live@example.com' ORDER BY created_at DESC LIMIT 1"]).toString().trim();
  const link = `https://app.perchmarkets.com/auth/magic-link#token=${token}`;
  await run('authLinks parses the emailed link', async () => { const t = magicLinkTokenFromUrl(link); if (t !== token) throw new Error('token mismatch'); return `${t.length} chars`; });
  await run('verifyMagicLink -> AuthSession', async () => { const s = await client.verifyMagicLink(token, 'Harness iPhone'); return `${s.account.email} plan=${s.account.entitlement.plan}`; });
  await run('getCurrentAccount', async () => { const a = await client.getCurrentAccount(); return a.id; });
  await run('getEntitlements', async () => { const e = await client.getEntitlements(); return `${e.plan}/${e.state}`; });
  await run('getToday', async () => { const t = await client.getToday(); return `${t.sessionDate} items=${t.items.length} first=${t.items[0]?.symbol} ${t.items[0]?.label}`; });
  let page;
  await run('getSignals limit=4', async () => { page = await client.listSignals({ limit: 4 }); return `items=${page.items.length} next=${page.nextCursor ? 'yes' : 'no'}`; });
  await run('getSignals next page', async () => { const p2 = await client.listSignals({ limit: 4, cursor: page.nextCursor }); return `items=${p2.items.length} next=${p2.nextCursor ? 'yes' : 'no'}`; });
  await run('getSignals origin=radar', async () => { const p = await client.listSignals({ origin: 'radar' }); return `items=${p.items.length} origins=${[...new Set(p.items.map(i => i.origin))]}`; });
  await run('getSignal detail', async () => { const d = await client.getSignal(page.items[0].id); return `${d.symbol} evidence=${d.supportingEvidence.length} weakening=${d.weakeningFactors.length} detectors=${d.evidenceSnapshot.detectors}`; });
  await run('getSignal unknown -> 404', async () => { try { await client.getSignal('00000000-0000-4000-8000-000000000000'); throw new Error('expected 404'); } catch (e) { if (e.status === 404) return e.code; throw e; } });
  await run('searchInstruments("ap")', async () => { const r = await client.searchInstruments('ap'); return r.items.map(i => i.symbol).join(','); });
  let wl;
  await run('getWatchlist (empty)', async () => { wl = await client.getWatchlist(); return `version=${wl.version} items=${wl.items.length}`; });
  await run('replaceWatchlist', async () => { wl = await client.replaceWatchlist([{ symbol: 'SPY', notificationsEnabled: true }, { symbol: 'IONQ', notificationsEnabled: false }, { symbol: 'AAPL', notificationsEnabled: true }], wl.version);
    return `version=${wl.version} ` + wl.items.map(i => `${i.symbol}:${i.instrument.assetType}:${i.quote ? '$' + i.quote.value + (i.quote.stale ? '(stale)' : '') : 'noquote'}`).join(' '); });
  await run('replaceWatchlist stale version -> 409', async () => { try { await client.replaceWatchlist([], 0); throw new Error('expected 409'); } catch (e) { if (e.status === 409) return e.code; throw e; } });
  await run('refresh via expired-access path', async () => { const t = await store.getTokens(); await store.setTokens({ ...t, accessTokenExpiresAt: new Date(0).toISOString() }); const a = await client.getCurrentAccount(); const t2 = await store.getTokens(); return `rotated=${t2.refreshToken !== t.refreshToken} account=${a.id.slice(0, 8)}`; });
  await run('registerDevice -> 501 http error (M2)', async () => { try { await client.registerDevice({ platform: 'ios', apnsEnvironment: 'sandbox', token: 'x', installationId: '9d2c1b9a-1b2c-4d3e-8f4a-5b6c7d8e9f00', appVersion: '1', locale: 'en', timeZone: 'UTC', preferences: { watchlistPriority: true, radarDiscoveries: false, corrections: true, operational: true, sessions: ['regular'], paused: false } }, '2f1e0d9c-8b7a-4655-9443-322110ffeedd'); throw new Error('expected 501'); } catch (e) { if (e.status === 501 && e.kind === 'http') return e.code; throw e; } });
  await run('logout', async () => { await client.logout(); return `tokens cleared=${(await store.getTokens()) === null}`; });
  console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL STEPS PASSED — real app client, real server');
  process.exit(failures ? 1 : 0);
})();
