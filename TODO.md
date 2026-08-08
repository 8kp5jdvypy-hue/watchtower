# Perch — operator to-do

Checklist of things **only you can do** (sudo prompts, GitHub auth,
physical machine, Telegram account). Everything else is already wired up.
Tick a box with `[x]` as you go.

---

## Do now — blockers for reliable unattended running

- [ ] **Schedule the pre-open wake.** In Terminal.app:
  ```
  sudo pmset repeat wakeorpoweron MTWRF 07:15:00
  pmset -g sched      # confirm a "Repeating power events" block appears
  ```
  Without this, the auto-start LaunchAgent only fires if the Mac is
  already awake at 7:20 MT. **Status: NOT set (checked — no repeating event).**

- [ ] **Keep the Mac on AC power with the lid open** during market hours.
  On battery, a closed lid or low charge can still sleep the machine and
  stall the scanner, even with caffeinate running.

- [x] **Auto-restart if a process crashes mid-day.** `scripts/watchdog.sh`
  now runs every 5 minutes via a LaunchAgent
  (`scripts/com.perch.watchdog.plist`, installed) and re-runs
  `start.sh` for anything that's down (the runner only during market
  hours). Still doesn't help if the Mac itself is asleep or off — that's
  what the wake + AC-power items above are for.

## Do soon — off-machine backup

- [ ] **Push the code to GitHub** (3 local commits not yet backed up:
  `d9c7062`, `78ad3cf`, `54d17de`). Auth isn't set up in this session.
  Pick one, run in Terminal.app:
  - PAT: `git push -u origin main` → username + a Personal Access Token
    (github.com/settings/tokens, "repo" scope) as the password, **or**
  - SSH: `ssh-keygen -t ed25519`, add the `.pub` at github.com/settings/keys,
    then tell me to switch the remote to SSH.

## Each trading day (until fully hands-off is confirmed)

- [ ] **Morning health check** after ~9:25 AM ET:
  ```
  scripts/status.sh      # want: all UP, heartbeat fresh, no incidents, "healthy"
  ```
  If anything's DOWN: `scripts/start.sh`.

- [ ] **Confirm the scanner caught the open.** First detection of the day
  should be ~9:30 ET, not late morning. (Today it started at 11:35 ET
  because it wasn't running before the open — the whole point of the
  wake + auto-start above.)

## Before the social-media launch

- [ ] **DM the bot `/start` from your own Telegram account** (not a
  channel) and walk the full onboarding once, as a user would see it.

- [ ] **Move off the laptop onto a VPS.** Dockerfile, docker-compose.yml,
  systemd units, and a backup script are already written and
  live-tested (`scripts/backup.sh` verified against the real databases
  2026-08-08) — see `docs/DEPLOYMENT.md` for the full setup checklist.
  What's still only-you-can-do: provision the VPS itself (any
  provider, 2 vCPU/4GB is plenty), and copy `.env` to it by hand
  (never via git). This is also where the GitHub push actually earns
  its keep — right now the VPS would have to pull from a local clone
  instead of `git clone`.

- [ ] **Document trades honestly**, including the losers — the whole
  design (status page, weekly recap, coin-flip disclosure) depends on the
  public record matching reality.

## Nice to have / later

- [ ] Ask me to wire `status.sh` into a monitor that pings you if the
  stack goes down mid-session (it already exits non-zero when unhealthy).
