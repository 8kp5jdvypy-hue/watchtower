"""Minimal, anonymous product-funnel logging.

Deliberately NOT a third-party analytics vendor: no Segment/Amplitude/
GA script on either frontend, one small first-party SQLite table (see
tradebot.telegram_bot.db's funnel_events schema), one write path, no
cookies set by this module. anon_id is a random value the frontend
generates and stores in localStorage — not derived from anything
identifying (no IP, no fingerprinting), and never joined against email
here. Once a visitor signs in, tradebot.api.app fills in account_id
from the session so a signup funnel can be traced end to end, but
nothing before that point is tied to a real person.

Exists to answer one question the rest of this codebase has no way to
answer today: does anyone actually make it from the landing page to a
signed-in session? Without this, every landing-page/CTA change is a
guess.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Every event this system will ever record — deliberately a small,
# reviewed allowlist (not "whatever the client sends") so a public,
# unauthenticated endpoint can never become an arbitrary write sink.
# Add here first, deploy, *then* start sending the new event.
ALLOWED_EVENTS = frozenset({
    "landing_view",       # perchmarkets.com pageview
    "signup_cta_click",   # any "Sign up" CTA, either site, before navigating away
    "login_cta_click",    # any "Log in" CTA, either site, before navigating away
    "magic_link_sent",    # POST /auth/magic-link/request succeeded (props: {mode})
    "app_authenticated",  # app.perchmarkets.com resolved a real session (once per page load)
})

MAX_PROPS_JSON_LEN = 500  # a few short key/value pairs, not a payload
MAX_ANON_ID_LEN = 64


@dataclass(frozen=True)
class FunnelEvent:
    id: int
    ts_utc: str
    event: str
    anon_id: str
    account_id: str | None
    props: dict | None


def record_event(
    conn: sqlite3.Connection,
    event: str,
    anon_id: str,
    account_id: str | None = None,
    props: dict | None = None,
) -> bool:
    """Returns False (and writes nothing) for anything outside
    ALLOWED_EVENTS or a missing anon_id. Callers should treat that as
    "silently ignored" rather than an error — the same anti-enumeration
    discipline tradebot.accounts uses for magic-link requests: a public
    endpoint should never behave observably differently for a bad
    request than a good one."""
    if event not in ALLOWED_EVENTS or not anon_id:
        return False
    props_json = None
    if props:
        encoded = json.dumps(props, separators=(",", ":"), sort_keys=True)
        if len(encoded) <= MAX_PROPS_JSON_LEN:
            props_json = encoded
    conn.execute(
        "INSERT INTO funnel_events (ts_utc, event, anon_id, account_id, props_json) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), event, anon_id[:MAX_ANON_ID_LEN], account_id, props_json),
    )
    conn.commit()
    return True


def counts_by_event(conn: sqlite3.Connection, since_iso: str | None = None) -> dict[str, int]:
    """A minimal read path — just enough to confirm the pipeline is
    actually recording something (e.g. from a shell), not a dashboard.
    Real funnel reporting (conversion rates between steps, time-to-
    convert) is future work once there's real volume to look at."""
    if since_iso:
        rows = conn.execute(
            "SELECT event, COUNT(*) FROM funnel_events WHERE ts_utc >= ? GROUP BY event", (since_iso,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT event, COUNT(*) FROM funnel_events GROUP BY event").fetchall()
    return dict(rows)
