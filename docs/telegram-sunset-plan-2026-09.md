# Telegram sunset plan — replace, then remove

**Date:** 2026-09-18
**Status:** approved direction (owner chose "replace, then remove" over
stopping Telegram now or deleting the code first). Decision entry in
`docs/DECISIONS.md`.
**Owner/approver:** Perch Markets operator

## The one fact that sizes this

The iOS MVP (`perch-mobile-mvp`, branch
`release/ios-mvp-zero-cost-20260910`) is built against a **`/v1/*`
contract that `api.perchmarkets.com` does not serve** — zero route
overlap today. The contract's source of truth is the app's own
`src/api/schemas.ts` (23 schemas, ~300 lines: `TokenPair`, `Account`,
`AuthSession`, `Today`, `SignalSummary`/`SignalPage`/`SignalDetail`,
`Watchlist`, `Device`, `NotificationPreferences`, `Entitlement`,
`InstrumentSearch`, `Methodology`, account export/deletion/reauth).
The backend has the *data* for all of it (journal, users, price feed,
universe) behind different routes and cookie sessions.

So "replace Telegram with the app" = build the `/v1` surface + an APNs
delivery channel, prove parity, then cut over. Telegram stays the
delivery channel until M3 passes.

## Milestones

**M0 — done 2026-09-18.** Perch restarted on `83ac9aa`; price feed
(`/prices`, `/status`, `/price`) live; Alpaca free plan end to end;
Telegram untouched.

**M1 — `/v1` read surface + auth (backend).** No app change needed:
the client already fails closed against these exact shapes.
*Status 2026-09-18: implemented on branch `feature/v1-ios-api-m1`
(`tradebot/api/v1.py`, `v1_store.py`). Gate evidence: (a) every
response validated with the app's own zod schemas via
`scripts/v1_schema_check.mjs` inside `tests/test_api_v1.py`; (b) the
app's real compiled client driven end to end against a local server
by `scripts/v1_live_client_check.cjs` — 21/21 steps including live
quotes in the watchlist. Not in M1: Sign in with Apple (needs Apple
identity-token verification + the owner's Team/Client IDs), account
export/deletion, StoreKit sync — each is a contract-shaped 501.
Two owner-side items before a device can sign in for real: the
dashboard Worker must serve `/.well-known/apple-app-site-association`
so `https://app.perchmarkets.com/auth/magic-link#token=…` opens the
app, and the SPA should show a "open in the app" page at that path
for browsers.*
- Auth: `/v1/auth/apple` (Sign in with Apple, server-side identity
  token verification), `/v1/auth/magic-link/{request,verify}`,
  `/v1/auth/refresh` → `TokenPair` (access + refresh, single-flight
  rotation is already on the client), `/v1/auth/logout`, `/v1/me`.
  New: an `apple` identity provider next to `telegram` in
  `linked_identities`; bearer tokens alongside the existing cookie
  session (the web app keeps cookies).
- Data: `/v1/status` (heartbeat + market state — same inputs as the
  bot's `/status`), `/v1/today` and `/v1/signals[/{id}]` mapped from
  `_recent_signals` + `signals_today` **with a `price` block from
  `tradebot.pricefeed` and its `stale` flag** — this is where "update
  the app with the price feed" lands — `/v1/watchlist` (versioned,
  server-confirmed; maps to `users_db` watchlist), `/v1/instruments`
  (over `universe.db`), `/v1/methodology` (static), `/v1/entitlements`
  (free tier, server-authoritative), `/v1/account/*` (export via the
  existing journal export; deletion with reauth proof).
- Gate: the app's `tests/apiClient.test.cjs` contract tests run green
  against a local Flask instance; owner signs in on a simulator build
  and sees real Today/Feed/Watchlist.

**M2 — push delivery (backend + owner's Apple key).**
- `/v1/devices` + `/v1/devices/{id}` — APNs token registry (the app
  already sends sandbox/production environment tags and rotates).
- `tradebot/push/apns.py` + an `apns-worker` compose service: direct
  APNs HTTP/2 with a `.p8` key (no third-party push vendor — zero
  cost). It reads the **same outbox** `TelegramAlerter.send()` writes,
  so every alert dual-runs: Telegram and APNs, same journal row, same
  `alert_id`. Delivery result per channel is recorded.
- Owner-only prerequisites: Apple Developer APNs auth key (`.p8`, Key
  ID, Team ID) placed in `/opt/perch/.env` via the console, same
  hidden-input method as `FINNHUB_API_KEY`. Nothing here creates it.

*M2 status 2026-09-19: implemented on branch `feature/m2-apns-push` —
`tradebot/push/{store,apns,delivery,worker}.py`, `/v1/devices` live,
`apns-worker` compose service (idles healthy until the APNS_* secrets
exist), runner composes the push hook with Telegram's so both channels
see every HIGH alert under the same alert_id. Owner-side to activate:
create an APNs auth key in the Apple developer portal (Keys → +, enable
APNs), then paste `APNS_KEY_P8_B64` / `APNS_KEY_ID` into the VPS `.env`
and `docker compose up -d`. `scripts/push_parity_report.py` is M3's
gate.*

**M3 — parity gate.** Ten live sessions where every Telegram-delivered
HIGH alert also landed via APNs on the owner's device within 60 s,
zero APNs-only failures, recorded per alert in the journal. TestFlight
build in the owner's hands for the whole window. This is the
evidence, not a feeling; no cut-over without it.

**M4 — cut Telegram.** In order: (1) `docker compose stop bot worker`
and remove the two services from `docker-compose.yml`; (2) delete the
18 BotFather commands and revoke the token (also closes the 2026-09-16
log-leak follow-up); (3) migrate `linked_identities` rows from
`telegram` to the account's Apple/email identity, default watchlist
for anyone unmigrated; (4) delete `tradebot/telegram_bot/`,
`TelegramAlerter`, and the 20 modules' references — a separate
reviewed PR, after (1)–(3) have run clean for a week.

## What stays independent of all this

- **Dashboard** (`app.perchmarkets.com`): one component reading
  `/prices` (already live) shows the crypto/equity prices. Half a day,
  any time, no dependency on M1–M4.
- Telegram keeps delivering alerts throughout M1–M3. Nothing in this
  plan degrades today's users until M4.

## Not decided here

- Whether M1's token auth replaces cookie sessions for the web app
  too (recommend: no, not yet).
- Paid entitlements/StoreKit (`/v1/billing/apple/sync`) — the app has
  the client side; the plan ships free-tier only until the owner
  decides pricing. The route returns the free entitlement until then.
