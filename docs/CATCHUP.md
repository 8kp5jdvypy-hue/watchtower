# Catch-up: Watchtower / Perch

Written 2026-08-29 for someone with **zero prior context**. Everything
here is sourced from the repository, from running its test suite, or
from read-only HTTP against production. Anything I could not confirm is
marked explicitly.

**What this document describes:** `origin/main` at
`48f0e59` ("Merge pull request #142 … feature/screening-session-archives").

> ⚠️ **Read this before you trust your working copy.** The local checkout
> at `~/projects/watchtower` is on `8c8ae5e` — **123 commits behind
> `origin/main`** — with uncommitted modifications to `CLAUDE.md`,
> `README.md`, `.gitignore` and untracked `AGENTS.md` + `scripts/verify.sh`.
> Reading files there will give you a picture of the project as it was
> around 2026-08-24. `git fetch && git log origin/main` before believing
> anything you read locally.

Evidence labels used below:

- **[V]** verified in this session (code read on `origin/main`, test run, live HTTP).
- **[S]** sourced — a claim made by a repo document, quoted, not re-measured.
- **[?]** uncertain or not determinable from this machine.

---

## 1. What it does

Perch (repo name `watchtower`, product name **perchmarkets.com**) is a
market scanner and alerting service for US equities. It watches the
market intraday, journals **every** detection it makes — including
sub-threshold ones it will never alert on — and pushes the strong ones
to Telegram. Users see their alert history and what price actually did
afterward on an authenticated web dashboard.

It is **read-only with respect to markets**: it never places orders and
has no broker write access. [V — `CLAUDE.md`, `README.md`, and no order
path exists in the code]

The product positioning is unusual and worth understanding, because it
drives a lot of the engineering: the pitch is *honesty*, not edge. The
README says so directly — "a discipline and journaling system built on a
technical alert feed — **not a proven trading edge**" — and states that
HIGH tier is currently **not statistically distinguishable from a coin
flip**. `/performance` in the bot recomputes that verdict live from the
journal every time it is asked. [V — `README.md`, `SCANNER_PLAN.md`]

### Markets and symbols watched

Two coverage layers:

**1. A fixed watchlist of 17 symbols** [V — `tradebot/config.py`]:

```
SPY QQQ GOOGL TSLA BE IONQ NVDA AAPL AMD META AMZN MSFT COIN PLTR SMCI IWM USO
```

> 🚩 **`CLAUDE.md` is stale here.** It still says the scanner watches
> "SPY, QQQ, GOOGL, TSLA, BE, IONQ" — the original six. The real list has
> been 17 since 2026-08-05, per the calibration comment in
> `detectors.py`. Don't take CLAUDE.md's summary line as the universe. [V]

**2. A broad scan over the entire active US equity universe.** A cheap
"Stage 1" screen (`tradebot/broad_scan.py`) runs every 30 minutes across
every active, tradable US equity/ETF discovered from Alpaca's asset
catalog (~13–14k symbols, OTC excluded), and **promotes up to 25** of the
strongest candidates into full "Stage 2" detector evaluation alongside
the fixed watchlist. Enabled with `--broad-scan`, which
`docker-compose.yml` passes in production. [V — `runner.py` argparse,
`universe.py`, `docker-compose.yml`]

This matters more than it sounds: the three most recent alerts on the
live public track-record endpoint (KXIN, DUO, OKTG) all carry
`"origin":"screening"` — they came from the broad scan, not the fixed
watchlist. [V — live HTTP, 2026-08-29]

Promotion only widens *coverage*. Promoted symbols go through the exact
same detectors, tiers, daily cap and cooldowns; the alert budget does not
increase. [V — `README.md` + `runner.py`]

### What triggers an alert

Seven pure detector functions. Six are "primary" (`DETECTORS`), one is
contextual (`CONTEXT_DETECTORS`). All thresholds are expressed in **ATR
units, never percentages** — a hard project rule. [V — `tradebot/detectors.py`]

| Detector | Fires when |
|---|---|
| `level_break` | Close first crosses prior high/low or the opening range by > 0.5 × ATR |
| `rvol_spike` | Cumulative session volume first crosses 3.0× the historical average for that time-of-day slot |
| `range_expansion` | Latest bar's high−low exceeds 2.0× trailing ATR(14) |
| `vwap_break` | Close first moves > 0.5 × ATR away from session VWAP, on the side it wasn't on before |
| `round_number_break` | Price crosses the nearest round number and closes ≥ 0.5 × ATR past it |
| `gap` | Session's first bar opens > 0.75 × the *prior session's* range away from prior close |
| `relative_strength_break` *(context)* | Symbol's return since the open diverges from SPY's by > 1.0 × ATR |

Most detectors fire **once** on the bar where a condition first becomes
true, not on every subsequent bar that remains true — deliberate, and
documented per-detector.

Detections firing on the same bar are merged into one **cluster**, scored
by `score_cluster()` (strongest individual score, plus a partial bonus
per corroborating detector), and the cluster score maps to a tier:

| Tier | Score | What happens |
|---|---|---|
| **HIGH** | ≥ 3.8 | Pushed immediately |
| **MEDIUM** | ≥ 1.9 | Batched into one digest per hour |
| **LOG** | below | Journaled only — **never** alerted |

Those thresholds are calibrated, not guessed: the comment block above
them records every recalibration pass, the cluster counts involved
(most recently 21,552 clusters across 143 cached sessions), and the
resulting rate — mean 3.62 HIGH/day. [V — `detectors.py:13-39`]

**Budget on top of tiering** (`tradebot/alerts.py`): max **8 HIGH per
day**, and a **45-minute cooldown** per `(symbol, detector_kind)`. Hitting
the cap emits one notice, then silence. Every suppression reason is
written to the journal. [V]

### Delivery channel

**Telegram is the alert channel.** Three cooperating processes, and the
split is deliberate:

- `tradebot.runner` and `telegram_bot.delivery` only ever **enqueue** to a
  durable outbox.
- `tradebot.telegram_bot.worker` is the **only** process that ever calls
  the Telegram send API — single-instance enforced by `flock()`, respects
  rate limits and priority.
- If the worker isn't running, **nothing reaches anyone**. [V — `README.md`, `alerts.py`]

The reason: the scanner's hot path must never block on a network call or
a retry loop.

Secondary surfaces: a **web dashboard** (magic-link email login via
Resend) showing signals, outcomes, performance and a trade journal; a
public **track-record API**; and a generated public **status page**.

The bot exposes 17 slash commands, verified against BotFather at startup
— the dispatcher **hard-fails** if the code's registry and BotFather's
registered list disagree in either direction. [V — `telegram_bot/commands.py`]

```
/start /status /performance /example /me /took /closed /limits /pause
/resume /watchlist /events /tiers /export /help /halt /feedback
```

---

## 2. Stack and architecture

### Languages and dependencies

**Backend:** Python 3.11+, type hints, dataclasses over dicts, "boring
code preferred". Deliberately stdlib-heavy — e.g. `metrics.py` documents
why it avoids a `statsd`/`prometheus_client` dependency. Direct deps only
(11 packages): `alpaca-py`, `pandas`, `numpy`, `exchange_calendars`,
`pandas_market_calendars`, `requests`, `boto3`, `pytz`, `tzdata`,
`flask`, `gunicorn`. [V — `requirements.txt`]

**Frontends:** two separate Vite + React 19 apps, no shared code or build.

- `web/` — the marketing site. Three.js / React Three Fiber / GSAP /
  Lenis; a cinematic scroll-driven front door.
- `web-app/` — the dashboard. Plain React, no animation libraries;
  explicitly "a utility surface, not the cinematic front door".

Lint is `oxlint` for both. Dashboard unit tests run on `node --test`; e2e
on Playwright. [V — `package.json` × 2, `web-app/README.md`]

### Core design rules (from `CLAUDE.md`, enforced throughout)

These are worth internalizing before changing anything:

1. **Detectors are pure.** Data in, `Detection | None` out. No I/O, no
   network, no clock reads, no globals.
2. **`Bar.ts` is the bar's OPEN, in UTC.** A 5-minute bar stamped 14:30 is
   not knowable until 14:35. Never make a decision timestamped before the
   bar it used has closed.
3. All market data goes through the `MarketData` protocol. **No vendor SDK
   imports outside its own adapter module** — `tradebot/vendors/alpaca.py`
   is the only file that imports the Alpaca SDK.
4. All thresholds in ATR units, never percentages.
5. Anchors are computed once per session and **frozen**, never recomputed
   per bar.
6. **Every detection is journaled before any alert is sent**, including
   sub-threshold ones.
7. Live alerting is opt-in via `--live`. Default is log-only.

### Data stores — five SQLite databases

The split is intentional; each answers a different kind of question. [V —
module docstrings, `scripts/backup.sh`]

| File | Holds |
|---|---|
| `data/journal.db` | What Perch **detected** — the track record. Detections, marks (outcomes), decision events |
| `data/users.db` | What a **user** did — accounts, subscriptions, trade journal, outbox |
| `data/universe.db` | What Perch is **allowed to look at** — the asset catalog, plus Stage 1 per-symbol screening events |
| `data/evaluations.db` | What the detectors **saw**, bar by bar, for symbols that reached Stage 2 |
| `data/postmarket_shadow.db` | Everything the default-off shadow observers record |

`universe.db` also carries a deliberate design note worth quoting: Stage 1
outcomes are *not* written to `decision_events`, because that ledger is
keyed on `detection_id` and a symbol screened out at Stage 1 has no
detection to key on. Minting a synthetic id "would destroy the one
property that table has — every row refers to a real detection." [V]

### Runtime topology

**Production is a single DigitalOcean VPS running Docker Compose.** Nine
services: eight built from one shared image, plus Caddy. `restart:
unless-stopped` is the watchdog. [V — `docker-compose.yml`]

| Service | Command | Role |
|---|---|---|
| `runner` | `tradebot.runner --live --broad-scan` | The scanner loop. Evaluates every 5-min bar, journals, alerts. Exits at the close |
| `bot` | `tradebot.telegram_bot.main` | Long-polls Telegram for commands and button taps |
| `worker` | `tradebot.telegram_bot.worker` | The only thing that calls Telegram's send API |
| `api` | `gunicorn tradebot.api.wsgi:app` | The Flask JSON API behind `api.perchmarkets.com` |
| `postmarket` | `tradebot.postmarket_shadow` | Postmarket earnings shadow observer — **default off** |
| `postmarket-discovery` | `tradebot.postmarket_discovery_shadow` | Market-wide postmarket discovery — **default off** |
| `postmarket-external-context` | `tradebot.postmarket_external_context_shadow` | News/options enrichment — **default off** |
| `postmarket-customer-dry-run` | `tradebot.postmarket_delivery_dry_run_shadow` | Delivery-readiness ledger — **default off** |
| `caddy` | `caddy:2.11.4-alpine` | The only service with published ports. TLS for `api.perchmarkets.com`, reverse-proxies `api` |

Each shadow service has its **own independent kill switch** defaulting to
`0`, so a routine deploy cannot start vendor polling by accident. The
runner's healthcheck is market-calendar-aware: pre-open, post-close,
weekends and holidays are *healthy idle*; staleness during a real session
fails closed.

**Frontends deploy separately** to two Cloudflare Workers static-asset
projects (not classic Pages):

| Worker | Source | Hostname |
|---|---|---|
| `watchtower` | `web/dist` | perchmarkets.com |
| `perch-dashboard` | `web-app/dist` | app.perchmarkets.com |

A documented gotcha: the zone cache is **not** in these Workers' request
path. `cf-cache-status: HIT` comes from the Workers assets platform's own
version-scoped cache, which a `wrangler deploy` supersedes atomically.
**There is no purge step**, and a zone Cache Rule against these hostnames
is inert. [S — `docs/STATE-OF-THE-SYSTEM.md`, which says it proved this live]

### The two-feed split — do not "clean this up"

`tradebot/vendors/alpaca.py` deliberately runs **two different Alpaca
feeds**, and `docs/PROGRAM-STATE.md` marks this as a guardrail:

- **Detector-facing calls** (`fetch_daily_bars`, `fetch_intraday_bars`,
  `fetch_daily_bars_bulk`) use `DETECTOR_DATA_FEED`, an env var
  **defaulting to `iex`**.
- **Quote-display calls** (`fetch_latest_quote`, `fetch_latest_quotes`)
  are **unconditionally SIP**, feeding the dashboard's `/quotes` endpoint
  only.

The reason it is not unified: `rvol_spike`'s volume baseline is
calibrated against IEX volume, which live measurement showed runs
**20–42× lower** than SIP's on this watchlist (SPY 26×, NVDA 20×, TSLA
42×, same session, same window). Flipping the detector feed to SIP without
a full baseline recalibration "would make `rvol_spike` fire on almost
everything." [V — code + docstrings; measurements are S]

---

## 3. Current state

### Verified working in this session

- **The full test suite is green: 1861 passed in ~40s** on `origin/main`,
  Python 3.11, no network access required. [V — I ran it]
- **Production is up and alerting.** `api.perchmarkets.com/healthz` → 200.
  `perchmarkets.com` and `app.perchmarkets.com` → 200.
  `/public/track-record` returns real production data; most recent
  delivered alert was **2026-08-28T17:16:36Z** (KXIN, origin `screening`).
  [V — live HTTP, 2026-08-29]

### Works end-to-end (per repo evidence)

- **Replay pipeline** — `--replay-date` runs the exact same
  detection/journaling code against cached sessions. This is described as
  the path most thoroughly demonstrated end-to-end.
- **Live scanning, journaling, tiering, budgeting, Telegram delivery**,
  including the durable outbox with a chaos test that `SIGKILL`s a real
  worker mid-broadcast and a load test draining 5,000 simulated chats.
- **Web dashboard** with magic-link auth (Resend), Today / Watchlist /
  Signals / Performance / Activity / Trade Journal, CSV export.
- **Outcome tracking** — every alert marked forward at +15/+30/+60 min and
  session close, with an append-only `mark_resolution_events` ledger
  giving each checkpoint an explicit state rather than an ambiguous blank.
- **Backups** — all five SQLite DBs, SHA-256 manifests, GPG-encrypted
  off-box shipping, isolated restore verifier, nightly systemd timer.
- **Deploy tooling** — `scripts/deploy.sh` requires one full SHA and
  verifies main ancestry, backups, per-service revisions, Compose health,
  SQLite integrity and public health.

### Stubbed, dormant, or uncalibrated

This is the part a newcomer most needs, because several things *look*
built and are not active:

| Thing | State |
|---|---|
| **Billing / payments** | **Stubbed.** `BillingProvider` is an interface raising `NotImplementedError`; the only implementation is `DevBillingProvider`, which writes `accounts.plan` directly. There is **no payment processor**. `STRIPE_PORTAL_URL` is wired but unused — the product is free during beta [V — `entitlements.py`] |
| **All four postmarket shadow services** | **Default off** (`POSTMARKET_*_ENABLED=0`). None can alert: they explicitly import no outbox, no Telegram, no broker, no delivery path [V — `docker-compose.yml`, module docstrings] |
| **Customer dry-run delivery** | **Dormant.** Even when enabled it additionally requires exact owner-authored policy *and* authorization files on disk [V] |
| **Licensed reference manifest** (float shares, true sector mapping) | **Dormant — no manifest exists** [S — `docs/postmarket-licensed-reference-manifest.md`] |
| `relative_strength_break`'s `atr_units=1.0` | **PLACEHOLDER.** The code comment says it "needs a replay calibration pass". Unlike every other threshold, this one was never calibrated [V — `detectors.py:485`] |
| `dedup.DEDUP_WINDOW_MINUTES=30`, `ESCALATION_SCORE_DELTA=2.0` | **Placeholders.** Comment: "need a replay-based frequency analysis … before trusting these as tuned values" [V — `dedup.py:22`] |
| Massive / Polygon second-provider paths | Code exists; **needs credentials** (`MASSIVE_API_KEY`, `MASSIVE_S3_*`). Shadow-only, never a delivery input [V] |
| `web/README.md` | Still the **unmodified Vite starter template**. Zero project content [V] |

### Merged to main but NOT live

- **Both Cloudflare Workers are badly stale.** The public track-record
  page (`web/record.html`, added 2026-08-17) has **never been live**:
  `https://perchmarkets.com/record.html` returns 3,278 bytes — byte-identical
  in size to `/` — i.e. the SPA shell, not the page. [V — live HTTP]
  A prior report measured both Workers' last deploy at **2026-08-16**,
  ~13 days behind main. [S — `docs/CATCHUP-2026-08-29.md`, via `wrangler`]
  Several merged dashboard improvements are therefore invisible to users.
- **The public status page is generated but served nowhere.**
  `api.perchmarkets.com/status.html` → **404**. `status_page.py` writes
  `data/status.html` on the VPS and its own docstring says the module
  never serves it. PR #137's work — exposing every operational failure
  family publicly — currently lands in a file no one can reach. [V]
- **[?] The VPS's running revision is unknown from this machine.** It is
  only readable from inside a container (`docker compose exec … code_version()`);
  no public endpoint exposes it, and there is no SSH access from this
  laptop. If you need to know what's actually deployed, that container
  command is the only way.

---

## 4. Open bugs and TODOs

### Confirmed open

| Item | Detail |
|---|---|
| **Uncalibrated thresholds** | `relative_strength_break` atr_units, and both `dedup` constants (above). These affect live alerting today |
| **UTC/ET bucketing bug** | `monthly_recap()` and `personal_stats()` in `telegram_bot/db.py` bucket trades by raw UTC `closed_at`, while *every other* day-boundary in the codebase converts to ET first. A trade closed at 8pm ET lands in the wrong day/month. Low severity today (no prominent UI), but will be silently inherited by whatever extends `user_trades` next [S — `docs/BACKLOG.md`, found 2026-08-13] |
| **Code-review findings #6, #10, #18, #21, #23, #25–#27** | Still open out of the original 27. #6's *destructive* half (mass-delist) is guarded, but the finding isn't fully closed [S — `docs/BACKLOG.md`] |
| **`RefreshResult.delisted == ()` is ambiguous** | Returns the same empty tuple for "nothing needed delisting" and "refused to delist because the fetch looked broken". The ERROR log distinguishes them; a caller can't [V + S] |
| **Near-close detection copy** | A detection fired close enough to the close that +15/+30/+60 fall after hours shows "Resolves after session close" — identical text to a genuinely pending row, but this one can *never* resolve. Should read "n/a — detected near close." Not implemented [S] |
| **Backup restore never tested against a real bucket** | The GPG/rclone restore legs have never been exercised end-to-end remotely. `DEPLOYMENT.md` says so itself. "A backup nobody has restored from is a hope, not a backup" [S] |
| **RFAMU-type thin-symbol policy** | A symbol can fire a cluster but be too thin for the vendor to return intraday bars at backfill time (confirmed real). Screen such symbols out, or accept occasional unresolvable marks? **Undecided — a human decision, not code** [S] |
| **First-session observation report** | Owed and never delivered: the read-only signal-rate / volume-multiple / error-log checklist against a real, cleanly-closed live SIP session [S] |
| **Open PR #73** (draft) | "runner.py: record the pipeline's real decisions in the ledger" — the only open PR [V — `gh`] |

### Stale documentation (found in this pass)

These are doc bugs, not code bugs, but each will mislead you:

1. **`CLAUDE.md` lists 6 watchlist symbols; the real list is 17.** [V]
2. **`docs/BACKLOG.md` still calls the "C3 remnant" open** — it says
   `scripts/fetch_cache.py` "exits 0 when a symbol ends its run with
   `gave up`". **It doesn't anymore**: the code returns 1 on that path
   (`fetch_cache.py:199`), and `docs/market-intelligence-program-2026-08.md`
   independently records it as resolved. BACKLOG.md is behind. [V]
3. **`tradebot/runner.py`'s module docstring says `--live` "has not been
   exercised against real live market conditions" and should be treated as
   unverified.** Production has been alerting live for weeks — the live
   track-record endpoint proves it. This docstring is a leftover from
   early development. [V]
4. **`docker-compose.yml`'s header comment says "caddy is the eighth
   service".** There are eight *app* services plus caddy = nine. [V]
5. **`docs/ROADMAP.md` still lists "Week 2 (Aug 18-24) — SIP cutover" as
   upcoming**, while `BACKLOG.md` opens with "the night of the SIP flip".
   The roadmap appears simply stale relative to a faster-than-planned
   cutover, but **[?] which document is authoritative was never resolved.**
6. **`docs/market-intelligence-program-2026-08.md` writes "merged and
   deployed at `<sha>`" for slices up to PR #93.** A prior report warns
   explicitly: **do not read that as the deployed revision** — it's
   doc-maintenance lag, since later PRs quote live production evidence
   from services that merged after #93. [S]

### Uncommitted work in the local checkout

Your working copy has changes that exist nowhere else — **they are not on
`origin/main` and will be lost if the checkout is discarded**: [V]

- `scripts/verify.sh` (untracked) — a repo-wide gate running pytest, both
  frontends' lint + build, dashboard unit tests, and `git diff --check`.
- `AGENTS.md` (untracked) — a copy of CLAUDE.md with a Workflow section.
- Modified `CLAUDE.md` / `README.md` referencing that `verify.sh`.

So the local CLAUDE.md instructs you to run `scripts/verify.sh` before
claiming repository-wide completion, but **that script does not exist on
`main`** — a fresh clone cannot follow its own instructions. Worth
committing or deliberately dropping.

---

## 5. Recent decisions and why

The last month is dominated by one theme: **the system got very good at
explaining itself, because a miss embarrassed it.**

### The triggering miss (2026-08-26)

CRWD, CRM and OKTA reported earnings and moved. Perch said nothing.
The postmortem established a clean causal chain — and notably, **nothing
was broken**: [S — `docs/market-intelligence-program-2026-08.md`]

```
active/tradable universe
  → no market-wide catalyst admission
  → not promoted into the 25-symbol Stage 2 set
  → no Stage 2 evaluation
  → no detection
  → no alert
```

Not an outage (full postmarket bars existed for all three). Not budget,
cooldown, or suppression. **No detection existed to route.** And the
runner terminates at the 16:00 ET close, so it couldn't have observed the
reaction even if the symbols had been promoted.

This produced ~65 merged PRs in three days across several deliberate
decisions:

**1. Record what the funnel did with *every* symbol, not just the winners.**
Stage 1 now persists a per-symbol outcome for all ~14k screened symbols
(aggregated when quiet), and Stage 2 persists what the detectors saw bar
by bar. A `miss_report` tool joins all four databases to answer "what did
Perch do around this event time?" *Why:* the widest part of the funnel —
thousands down to a couple dozen — is where a miss is most likely, and
until then nothing was recorded there at all.

**2. Everything new is shadow-only until an evidence gate passes.**
The signal-quality program's stated rule: "All new capabilities remain
shadow-only until the evidence gate passes. No quality score may be
described as confidence, probability, profitability, or advice unless its
calibration supports that exact claim." Gates emit only `NOT_READY` or
`ELIGIBLE_FOR_OWNER_REVIEW` — **there is no activation path in code.**
Campaigns must be *preregistered*: the range, floors, feeds and eligible
revisions are locked before the first covered session, and the final
evidence package SHA-256-pins that campaign. *Why:* it makes "we tuned
until it looked good" structurally impossible.

**3. Refund the alert budget when the data guard rejects an alert.**
Real incident, 2026-08-26: eight HIGH candidates failed data-integrity
validation but **consumed all eight daily reservations**. Only one HIGH
was actually delivered, and a later valid HIGH was suppressed as
`daily_cap_reached`. `release_unsent()` now undoes the reservation —
"an alert nobody received must not count as sent." [S — PR #84]

**4. Key volume baselines by wall-clock time, not list position.**
`rvol_spike` and `relative_strength_break` used to index history by
position in the bar list. One silently dropped vendor bar would shift
every later comparison five minutes earlier — permanently, for the rest
of the session, with no error. Now both use DST-aware ET slot alignment
and exact-timestamp joins, failing closed on missing or duplicate
timestamps. *Why it mattered:* "statistical, not a crash — exactly the
kind of bug that wouldn't announce itself." [V — the diff; S — the framing]

**5. Make ambiguous silence impossible.** A recurring pattern across
many of these PRs: a function returning the same value for "fine" and
"broken" is treated as a defect in its own right. Examples — a missing
cache file no longer becomes `[]`; an empty vendor fetch can no longer
mass-delist the universe (guarded below a 50% plausibility floor); option
chain failures no longer look identical to an honest "no contract";
schema migration now only swallows SQLite's *exact* duplicate-column
error and stops startup on lock/disk/I-O errors; `metrics.json` is
published atomically and corrupt bytes are preserved rather than
overwritten.

**6. Commit the journal before any send that references it.**
A Telegram alert could be durably enqueued *before* the detection row it
cites committed to `journal.db` — a crash in that window delivered a real
subscriber alert citing a `detection_id` that never existed. Routed
through `_commit_then_send()`, making the bad ordering inexpressible
rather than a convention to remember. [S — PR #36]

### Standing product rules (from `docs/ROADMAP.md`)

Treat these as constraints, not preferences — the roadmap gives them "the
same weight as 'no fabricated data'":

- Premium niche positioning: win on **price and trust**, not volume. "The
  honest one."
- Anchor pricing $75–150/mo, annual billing with a real discount.
- **No lifetime deals, no broker affiliate revenue, no discount-code
  influencer marketing — ever.**
- The journal is the content factory: weekly public recap, **unedited,
  misses included**.
- Engine/threshold changes require **evidence + separate approval**.
  Proposals before code. Archives, never deletes.

---

## 6. How to run it locally

### Prerequisites

**Python 3.11+ is required and enforced.** ⚠️ On this machine the default
`python3` is **3.9.6** — too old. There is a `python3.11` at
`~/.local/bin/python3.11`, but it has none of the project's dependencies
installed. Use an explicit venv. [V]

```bash
cd ~/projects/watchtower
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # pytest on top of requirements.txt
```

### Run the tests

```bash
python3 -m pytest -q
```

Expect **1861 passed** in roughly 40 seconds. [V — measured on `origin/main`]

No test touches the network, a real Telegram bot, or real market data.

If you have the local `scripts/verify.sh` (see §4 — it is uncommitted),
that additionally runs both frontends' lint and build:

```bash
scripts/verify.sh
PYTHON_BIN=/path/to/python3.11 scripts/verify.sh   # if python3 is older
```

### Credentials

Create a `.env` in the repo root (never committed). Minimum for a live
run: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALPACA_KEY_ID`,
`ALPACA_SECRET_KEY`. Every long-running process sources it the same way:

```bash
set -a && source .env && set +a
```

Notable optional vars: `DETECTOR_DATA_FEED` (`iex` | `sip`, **defaults to
`iex`**), `SESSION_SECRET_KEY` (required by the API — it raises at startup
without one), `SESSION_COOKIE_SECURE=0` for a plain-HTTP local API,
`RESEND_API_KEY`/`RESEND_FROM_EMAIL` for magic-link email,
`SEC_EDGAR_USER_AGENT`, and the four `POSTMARKET_*_ENABLED` switches (all
default `0`). Full table in `README.md`.

### Run the scanner — the safe way first

**Replay against cached sessions. No credentials, no market hours, no
alerts sent:**

```bash
python3 -m tradebot.runner --replay-date 2026-08-05
```

This runs the *exact same* pipeline as live mode, fast-forwarded against
`data/cache/`. It is the recommended way to see the system work.

Safety properties worth knowing: `--replay-date` **cannot** be combined
with `--live` (argparse rejects it — a replay would push hours-stale
alerts and a morning briefing to real subscribers). A replay also defaults
to `data/journal_replay.db` and `data/metrics_replay.json`; naming the
production journal is refused unless you pass
`--allow-production-replay-db`. [V — `runner.py:main()`]

**Log-only live mode** (default — prints alerts to the console, sends
nothing):

```bash
python3 -m tradebot.runner
```

**Actually live** requires `--live` plus all three processes:

```bash
set -a && source .env && set +a
python3 -m tradebot.telegram_bot.worker &   # must run, or nothing is delivered
python3 -m tradebot.telegram_bot.main &     # commands
python3 -m tradebot.runner --live --broad-scan &
```

Useful flags: `--no-personal-alerts` (ops channel only, skip per-user DMs),
`--broad-scan` (Stage 1 universe screen — production uses it),
`--db-path` / `--cache-dir` (for A/B comparisons; see `scripts/compare_replay.py`).

On macOS there is a wrapper kit — `scripts/start.sh` (idempotent, adds a
`caffeinate` guardian so a sleeping Mac can't freeze the scanner),
`scripts/status.sh` (exits non-zero when something's wrong, so it doubles
as a cron probe), `scripts/stop.sh`.

### Run the API and dashboard

```bash
# API on :8000 — the invocation the code itself documents
set -a && source .env && set +a
SESSION_COOKIE_SECURE=0 gunicorn tradebot.api.wsgi:app -b 0.0.0.0:8000

# Dashboard
cd web-app && cp .env.example .env.local && npm install && npm run dev

# Marketing site
cd web && npm install && npm run dev
```

`VITE_API_URL` is baked in **at build time**, not read at runtime.

Note that importing `tradebot.api.wsgi` opens real sqlite connections to
`data/users.db` and `data/journal.db` as a side effect — deliberate, and
the reason tests import `create_app` from `tradebot.api.app` directly with
`tmp_path` databases instead. [V]

### Stopping a live run

Prefer the HALT file over `kill` — the loop notices it between bars and
sends a shutdown notice:

```bash
touch data/HALT
# ...wait for it to exit...
rm data/HALT      # nothing removes this automatically
```

There are four independent kill switches at different scopes: a user's
own `/halt`, an admin's global `/halt`, the `data/HALT` file, and
`WATCHTOWER_KILL_SWITCH` (worker only). [V — `README.md`; note that table
is headed "Three independent ways" but lists four rows]

### ⚠️ Do not do these without explicit approval

Per `CLAUDE.md`'s workflow rules: do not deploy, change production
configuration, rotate credentials, alter data-provider entitlements, or
perform destructive production actions. Also, per `docs/PROGRAM-STATE.md`:
**do not fold the SIP display path into the detector feed config** — see
§2's two-feed note.

---

## 7. Where to read next

Ordered by usefulness to a newcomer:

1. **`README.md`** — the operational manual: processes, env vars, runbook,
   kill switches, test taxonomy.
2. **`CLAUDE.md`** — the engineering rules (but see §1 on its stale
   watchlist line).
3. **`SCANNER_PLAN.md`** — detector architecture, and the honest
   train/test-validated writeup of what has and hasn't held up.
4. **`docs/PROGRAM-STATE.md`** — short, and the closest thing to a list of
   things you must not casually change.
5. **`docs/ROADMAP.md`** — strategy and standing rules.
6. **`docs/STATE-OF-THE-SYSTEM.md`** — a thorough 2026-08-15 handoff.
   Excellent, but predates ~100 commits.
7. **`docs/CATCHUP-2026-08-29.md`** — *(untracked, local checkout only)* a
   very detailed PR-by-PR report on #78–#142. Written for a reader who
   already knew the project up to PR #77.
8. **`docs/market-intelligence-program-2026-08.md`** and
   **`docs/signal-quality-program.md`** — the active programs driving
   current work.
9. **`docs/BACKLOG.md`** — open items, with the staleness caveat in §4.

---

## 8. Honest limits of this document

- **No VPS access from this machine**, so the deployed revision and the
  live values of `DETECTOR_DATA_FEED` and the `POSTMARKET_*` switches are
  **unknown**. Everything about the VPS here is either public HTTP or
  quoted from a repo document.
- **I did not run `wrangler`.** The claim that both Workers last deployed
  on 2026-08-16 is sourced from `docs/CATCHUP-2026-08-29.md`. What I
  independently verified is narrower but sufficient: `record.html` serves
  the SPA shell, so the track-record page is definitely not live.
- **The frontend test suites were not run** — only the Python suite.
- Statements labeled **[S]** are repo claims I did not re-measure. Where a
  repo document and the code disagreed, I read the code and said so (§4).
