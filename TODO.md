# Perch — operator to-do

Checklist of things **only you can do** (sudo prompts, GitHub auth,
account signups, Telegram account). Everything else is already wired up.
Tick a box with `[x]` as you go.

---

## Done — the laptop-era checklist

These no longer apply now that the bot runs on a VPS instead of this
Mac (2026-08-10):

- [x] Push the code to GitHub — `main` is the default branch, fully
  pushed and up to date.
- [x] Move off the laptop onto a VPS — DigitalOcean droplet at
  `67.207.83.138`, all 5 services (`worker`/`bot`/`runner`/`api`/`caddy`)
  running via `docker compose`, `perch.service`/`perch-backup.timer`
  enabled so it survives a reboot.
- [x] Point `api.perchmarkets.com` at the VPS — DNS-only record in
  Cloudflare, Caddy has a real Let's Encrypt certificate.
- ~~Schedule the pre-open wake~~ / ~~keep the Mac on AC power~~ / ~~morning
  health check on the laptop~~ — moot, nothing runs on this laptop
  anymore. (`scripts/status.sh` still works if you ever want to check
  this machine specifically, but it's no longer the live host.)

## Done — web dashboard (2026-08-10)

- [x] **Deploy `web-app/`** to its own Cloudflare Workers project
  (`perch-dashboard`) — live at `app.perchmarkets.com`.
- [x] **Resend domain verified**, `RESEND_API_KEY` / `RESEND_FROM_EMAIL`
  set on the server. Confirmed working end-to-end: magic-link email
  sent, received, clicked, logged into the real dashboard.

The full surface is live: `perchmarkets.com` (marketing), `app.perchmarkets.com`
(dashboard), `api.perchmarkets.com` (backend), plus the Telegram bot —
all pointing at the one VPS.

## Before the social-media launch

- [ ] **DM the bot `/start` from your own Telegram account** (not a
  channel) and walk the full onboarding once, as a user would see it.

- [ ] **Document trades honestly**, including the losers — the whole
  design (status page, weekly recap, coin-flip disclosure) depends on the
  public record matching reality.

## Ongoing, on the server now (not this laptop)

- [ ] Occasional health check: `ssh root@67.207.83.138`, then
  `cd /opt/perch && docker compose ps` — want all 5 services `Up`.

## Nice to have / later

- [ ] Ask me to wire `status.sh` (or an equivalent on the server) into a
  monitor that pings you if the stack goes down.
