# Watchtower

A discipline and journaling system built on a technical alert feed — not
a proven trading edge. It watches a fixed watchlist for notable
intraday conditions, journals every detection (including sub-threshold
ones), and pushes HIGH-tier alerts to Telegram. **Free during beta.**

Read-only: it never places orders and has no broker write access. See
`CLAUDE.md` for the engineering rules this codebase follows and
`SCANNER_PLAN.md` for the detector architecture and the honest,
train/test-validated writeup of what has and hasn't held up in this
project's own data — including why HIGH tier is currently **not**
statistically distinguishable from a coin flip. `/performance` and
`/start` in the bot recompute that verdict live, every time, from the
journal — nothing here is a number the journal can't reproduce on
demand.

## Architecture at a glance

Three independent long-running processes, sharing only `data/*.db` and
a couple of small state files on disk:

| Process | What it does |
|---|---|
| `python3 -m tradebot.runner --live` | The market-scanner loop: evaluates detectors every 5-minute bar, journals every cluster, sends HIGH alerts. Runs once per trading day and exits at the close. |
| `python3 -m tradebot.telegram_bot.main` | Long-polls Telegram for commands (`/start`, `/status`, `/me`, ...) and button taps. |
| `python3 -m tradebot.telegram_bot.worker` | The only process that ever calls the Telegram *send* API. Drains the outbox (see "Outbox and delivery" below) respecting rate limits and priority. |

`tradebot.runner` and `tradebot.telegram_bot.delivery` only ever
*enqueue* — if the worker isn't running, nothing reaches anyone. All
three must be running for live alerting to actually work end to end.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # adds pytest on top of requirements.txt
```

Everything else is Python standard library — deliberate, see e.g.
`tradebot/metrics.py`'s docstring on avoiding a `statsd`/`prometheus_client`
dependency for a bot this size; the same call was made throughout.

For running this on a VPS instead of a laptop (Docker Compose +
systemd + backups), see `docs/DEPLOYMENT.md` — everything below this
point describes the bare-metal/macOS dev setup.

Create a `.env` in the repo root (never committed — see `.gitignore`)
with at least `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` — see Environment variables below
for the full list. Every long-running process sources it the same way:

```bash
set -a && source .env && set +a
```

## Running

### Quick start (recommended) — the wrapper kit

Three scripts in `scripts/` bring the whole live stack up and down and
keep this Mac from sleeping while it runs (a sleeping Mac freezes all
three processes, so the scanner stops evaluating bars — see the
`caffeinate` note below):

```bash
scripts/start.sh                 # start worker + bot + runner + caffeinate guardian
scripts/start.sh --sync-commands # same, but also push commands.py to BotFather first
scripts/status.sh                # one-glance health: procs, heartbeat age, HALT, incidents, power
scripts/stop.sh                  # graceful SIGTERM to all (add --force to SIGKILL stragglers)
```

`start.sh` is idempotent — a process already running is left alone, not
doubled. It writes a pidfile per process under `data/*.pid`, which
`status.sh` and `stop.sh` read. `status.sh` exits non-zero when
something's wrong (a process down, a stale heartbeat during market
hours, or an open incident), so it doubles as a monitor/cron probe.

**caffeinate + power.** `start.sh` launches `caffeinate -dimsu -w <runner
pid>`, which holds an idle-sleep assertion for exactly as long as the
scanner lives. This blocks *idle* sleep, but on **battery** a closed lid
or low charge can still sleep the Mac. For a reliable market-hours run:
**plug into AC power and keep the lid open.** `status.sh` warns when
you're on battery.

### Auto-start before the market open (launchd)

`scripts/com.watchtower.kestrel.autostart.plist` is a LaunchAgent that
runs `start.sh` at **7:20 AM America/Denver, Mon-Fri** (9:20 AM ET, 10
min before the open). It survives reboots. Install it once:

```bash
cp scripts/com.watchtower.kestrel.autostart.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.watchtower.kestrel.autostart.plist
# run it now to confirm it works (idempotent — won't double anything):
launchctl kickstart -k gui/$(id -u)/com.watchtower.kestrel.autostart
launchctl print gui/$(id -u)/com.watchtower.kestrel.autostart | grep -E "state|last exit code"
```

To remove it: `launchctl bootout gui/$(id -u)/com.watchtower.kestrel.autostart`.
Its output is captured to `data/launchd_autostart.log`.

**Waking the Mac for it.** launchd fires the job on schedule but will
NOT wake a sleeping Mac — and because the scanner exits at the market
close each day (taking its caffeinate guardian with it), the Mac can
sleep overnight and miss the 7:20 trigger. Schedule a wake a few minutes
earlier (requires `sudo`, run in a real Terminal):

```bash
sudo pmset repeat wakeorpoweron MTWRF 07:15:00   # wake weekdays at 7:15 MT
pmset -g sched                                   # verify the repeating wake
```

With the wake + the LaunchAgent, the stack comes up unattended before
every open. Keep the Mac plugged into AC power so the wake isn't blocked
on battery.

### Auto-restart if something crashes mid-day (launchd watchdog)

`scripts/watchdog.sh` checks every 5 minutes whether the bot, worker, or
(during market hours only) the runner have died, and re-runs `start.sh`
— already idempotent — to bring back only what's missing. This is what
actually recovers from a mid-session crash; the autostart LaunchAgent
above only covers the pre-open start. Install it once:

```bash
cp scripts/com.perch.watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.perch.watchdog.plist
launchctl print gui/$(id -u)/com.perch.watchdog | grep -E "state|last exit code"
```

To remove it: `launchctl bootout gui/$(id -u)/com.perch.watchdog`. Its
output goes to `data/watchdog_launchd.log`; every restart it triggers is
also logged to `data/watchdog.log` and recorded as a short, already-
resolved entry in the incident log (`data/incidents.jsonl`) so it shows
up in the history, not just the log file.

**This is a stopgap, not production infrastructure.** It only helps if
the Mac itself is awake and on AC power — see the `pmset`/LaunchAgent
caveats above. The real fix (process supervision on a real server that
doesn't depend on a laptop staying open) is planned separately; this
watchdog exists to close the gap in the meantime.

The manual, one-process-at-a-time commands below still work and are what
the kit runs under the hood — use them when you need to restart a single
process.

### Replay (backtest against cached sessions — no live market needed)

```bash
python3 -m tradebot.runner --replay-date 2026-08-05
```

Runs the exact same detection/journaling pipeline as live mode,
fast-forwarded against `data/cache/`. This is the path that's actually
been run end-to-end and demonstrated — see the module docstring in
`tradebot/runner.py`.

### Live

```bash
set -a && source .env && set +a
nohup python3 -m tradebot.runner --live > data/runner_live.log 2>&1 &
disown
```

Default is log-only (`ConsoleAlerter`) unless `--live` is passed with
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set, in which case alerts route
through the outbox to the ops channel and to subscribers.

Add `--broad-scan` (on by default in `scripts/start.sh`) to also run
Stage 1 (`tradebot.broad_scan`) across the full active universe
(`tradebot.universe`, discovered from Alpaca's asset catalog — live-
verified 2026-08-08 at ~13,000 active US equities/ETFs excluding OTC)
every 30 minutes, promoting up to 25 of the strongest cheap-screen
candidates into live Stage 2 evaluation alongside the fixed `WATCHLIST`.
This only widens *coverage* — every promoted symbol still goes through
the exact same detectors, tiers, daily HIGH cap, and cooldowns as
everything else; it does not raise the alert budget. Off by default if
you invoke `tradebot.runner` directly without the flag.

### Command bot

```bash
set -a && source .env && set +a
nohup python3 -m tradebot.telegram_bot.main > data/telegram_bot.log 2>&1 &
disown
```

Add `--sync-commands` on the FIRST run after changing
`tradebot/telegram_bot/commands.py`'s command list — the dispatcher
hard-fails at startup (`CommandDriftError`) if the code's command
registry and BotFather's registered commands disagree, in either
direction. `--sync-commands` pushes the local list to BotFather via
`setMyCommands` before continuing; only needed once per change, not on
every restart.

### Outbox delivery worker

```bash
set -a && source .env && set +a
nohup python3 -m tradebot.telegram_bot.worker > data/outbox_worker.log 2>&1 &
disown
```

Single-instance guaranteed via a `flock()` on `data/outbox_worker.lock` —
a second instance refuses to start rather than double-sending. `--once`
drains everything currently pending and exits (used by tests, not
production).

## Runbook

### Checking what's running

```bash
ps aux | grep -E "tradebot\.(runner|telegram_bot\.main|telegram_bot\.worker)" | grep -v grep
```

### Restarting the command bot or the worker

Both are plain long-polling/loop processes with no other graceful-stop
mechanism than SIGTERM (the worker handles SIGTERM gracefully — finishes
the current batch before exiting; `telegram_bot.main` does not
special-case it, but a `get_updates` long-poll in flight is not
work-in-progress that can be corrupted).

```bash
kill -TERM <pid>
# wait for it to exit, then:
set -a && source .env && set +a
nohup python3 -m tradebot.telegram_bot.worker > data/outbox_worker.log 2>&1 &
disown
```

### Stopping the live scanner (`tradebot.runner --live`)

Prefer the HALT file (graceful — the loop notices it between bars and
sends a shutdown notice) over `kill`:

```bash
touch data/HALT
# ... wait for the loop to notice and exit ...
rm data/HALT   # do NOT forget this — see Kill switches below
```

A live session also ends on its own at the market close; restarting it
after that just re-exits immediately until the next trading day.

### Regenerating the public status page

```bash
python3 scripts/generate_status_page.py            # writes data/status.html
python3 scripts/generate_status_page.py --output /path/to/hosted/status.html
```

Reads only from the journal, `tradebot.incidents`, and `tradebot.metrics`
— every number is reproducible on demand. This only writes the file; it
does not serve it. Run on a schedule (cron) or by hand after an incident
resolves, and host the output wherever's convenient (GitHub Pages, S3,
nginx).

## Kill switches

Three independent ways to stop alerts, at different scopes:

| Mechanism | Scope | How |
|---|---|---|
| `/halt` (regular user) | That user's alerts, for the rest of today's session | Telegram command; `/resume` lifts it early |
| `/halt` (admin) | Everyone, until manually lifted | Telegram command — writes `data/HALT` |
| `data/HALT` file | Everyone | `touch data/HALT` — the live loop and the outbox worker both check for it and stop; **remove it manually to resume** (nothing does this automatically) |
| `WATCHTOWER_KILL_SWITCH` env var | The outbox worker only | Set to any truthy value (`1`, `true`, `yes`, `on`) and restart the worker — it will refuse to deliver. Composed with the HALT file check into one `stop_check_fn` (see `tradebot/telegram_bot/worker.py`) |

A halt is logged to `tradebot/incidents.py`'s append-only log (kind
`"halt"`) and shows up in the public status page's incident log. It has
no in-process "resume" moment — closing that incident happens
automatically at the top of the *next* `run_live()` call, since reaching
that point at all is proof the system came back (see
`tradebot.incidents`' module docstring).

## Environment variables

**Credentials**

| Var | Required for | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `--live`, the command bot, the worker | From BotFather |
| `TELEGRAM_CHAT_ID` | `--live` | The ops/alerts chat the bot broadcasts to (system notices, morning briefing, weekly recap, the pinned status message). If this is a real Telegram *channel* (not a group), commands typed into it carry no sender identity at all — see `CHANNEL_COMMANDS_ENABLED` below and `/start`'s DM-only requirement |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | live market data | `tradebot/vendors/alpaca.py` is the only module that imports the Alpaca SDK |
| `SEC_EDGAR_USER_AGENT` | EDGAR filing checks | SEC requires a real contact in the User-Agent; requests without one get rate-limited or blocked. Defaults to a placeholder that identifies as this project |

**Bot configuration**

| Var | Default | Purpose |
|---|---|---|
| `ADMIN_TELEGRAM_IDS` | none | Comma-separated Telegram user IDs who can run the admin `/halt` (global) |
| `ALLOWED_USER_IDS` | unrestricted | Comma-separated Telegram user IDs; if set, everyone else gets "invite-only" |
| `CHANNEL_COMMANDS_ENABLED` | `false` | Whether read-only commands (`/status`, `/performance`, `/help`) respond when posted directly into the alerts channel |
| `WATCHTOWER_MAX_USERS` | unlimited | Config cap on total onboarded users — once hit, a brand-new `/start` lands on a waitlist instead of onboarding. Protects the bot's own operational scale, not Telegram's send-rate limits (those are independently enforced by the outbox worker's token buckets regardless of subscriber count) |
| `STRIPE_PORTAL_URL` | unset | Billing portal link for `/tiers`. Unused during beta — no payments are collected — but the seam is wired so this is a one-line change later |
| `SUPPORT_CONTACT` | `@support` | Shown in `/help` and `/tiers` |
| `LOG_LEVEL` | `INFO` | Python logging level for `telegram_bot.main` |

**Kill switch / ops**

| Var | Purpose |
|---|---|
| `WATCHTOWER_KILL_SWITCH` | See Kill switches above |

**Test-only overrides** (never set these in a real deployment)

| Var | Purpose |
|---|---|
| `TELEGRAM_API_ROOT` | Points the outbound sender at a fake local HTTP server instead of `https://api.telegram.org` — used by the chaos test |
| `OUTBOX_LEASE_TIMEOUT_SECONDS` | Overrides the outbox's stale-lease reclaim timeout (default 60s) so tests don't wait out a real minute |

## Testing

```bash
python3 -m pytest -q
```

- **Golden-file tests** (`tests/test_templates.py` and others) snapshot
  every rendered message exactly, so a formatting change shows up as a
  diff instead of surprising someone in Telegram.
- **Validator tests** (`tests/test_guard.py`) — one test per
  data-integrity rejection rule in `tradebot/guard.py`, the check every
  alert must pass before publish.
- **Integration test** (`tests/test_integration_pipeline.py`) walks a
  real signal through `runner.process_new_bar` → `guard.validate_alert_data`
  → `templates.render_high_alert` → the real outbox → a real
  `WorkerCore` drain, with only the Telegram HTTP call mocked — every
  other layer is production code, not a stub.
- **Chaos test** (`tests/telegram_bot/test_chaos_worker_crash.py`) kills
  a real worker subprocess mid-broadcast with `SIGKILL` and confirms no
  losses and at most one documented duplicate.
- **Load test** (`tests/telegram_bot/test_load_5000_chats.py`) drains
  5,000 simulated chats against an independent rate-limit oracle without
  ever tripping it.

No test suite run is required to touch the network, a real Telegram
bot, or real market data.
