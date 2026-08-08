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

- [ ] **Decide on hosting.** The bot currently depends on this one laptop
  staying awake and online. For a public launch, consider moving the
  three processes to a cheap always-on server so uptime doesn't hinge on
  your machine. (Ask me to sketch this when ready — it's where the GitHub
  push actually earns its keep.)

- [ ] **Document trades honestly**, including the losers — the whole
  design (status page, weekly recap, coin-flip disclosure) depends on the
  public record matching reality.

## Nice to have / later

- [ ] Ask me to wire `status.sh` into a monitor that pings you if the
  stack goes down mid-session (it already exits non-zero when unhealthy).
