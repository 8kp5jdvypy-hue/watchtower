# Perch dashboard

The authenticated web dashboard — Today / Watchlist / Recent Signals /
Performance / My Activity. Deliberately not a trading terminal (see the
plan doc): read-only views over `tradebot/api/app.py`, magic-link login,
no order placement, nothing live-streamed.

Separate app from the marketing/waitlist site (`web/` on the
`worktree-kestrel-waitlist` branch) — different Cloudflare
Workers/Pages project (`perch-dashboard` vs `watchtower`), different
domain (`app.perchmarkets.com` vs `perchmarkets.com`), no shared code or
build. Plain Vite/React, no Three.js/GSAP/scroll-driven animation — this
one's a utility surface, not the cinematic front door.

## Local development

```bash
cp .env.example .env.local     # points VITE_API_URL at localhost:8000
npm install
npm run dev
```

Run `tradebot/api/app.py` locally alongside it (see the main repo's
`docs/DEPLOYMENT.md` for the env vars it needs — at minimum
`SESSION_COOKIE_SECURE=0` for a plain-HTTP local API).

## Deploy

Same pattern as the marketing site: Cloudflare Workers static assets
(see `wrangler.toml`), not classic Pages.

```bash
npm run build
npx wrangler deploy
```

Set `VITE_API_URL=https://api.perchmarkets.com` as a build-time
environment variable in the Cloudflare project settings (or export it
before `npm run build` if deploying by hand) — it's baked into the
bundle at build time, not read at runtime.
