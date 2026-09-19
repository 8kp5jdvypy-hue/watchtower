"""Fan-out of one HIGH alert to registered devices, via the push outbox.

make_push_subscriber_hook returns the same `(cluster, text, entry_mid)`
callable shape runner.process_new_bar expects, so runner composes it
with the Telegram hook (compose_hooks) and both channels see every
alert with the same alert_id (cluster.id). Eligibility here is the
/v1 model: the account's watchlist carries the symbol with
notifications on, the device is active, its preferences are not paused
and include the alert's session, and the local quiet window (if set)
is not in effect.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tradebot.push import store

logger = logging.getLogger("watchtower.push.delivery")

SESSION_FOR_V1 = {"premarket": "premarket", "regular": "regular", "after_hours": "after_hours"}
CATEGORY = "PERCH_OBSERVATION"


def _session_of(ts: datetime) -> str:
    from tradebot.api.v1 import _session_label

    return _session_label(ts)


def _in_quiet_window(prefs: dict, ts: datetime, tz_name: str | None) -> bool:
    start, end = prefs.get("quietStartLocal"), prefs.get("quietEndLocal")
    if not start or not end:
        return False
    try:
        local = ts.astimezone(ZoneInfo(tz_name or "America/New_York"))
    except Exception:  # noqa: BLE001 -- unknown zone: don't silently suppress
        return False
    hhmm = local.strftime("%H:%M")
    return (start <= hhmm < end) if start <= end else (hhmm >= start or hhmm < end)


def eligible_devices(conn, symbol: str, ts: datetime) -> list[store.Device]:
    session = _session_of(ts)
    accounts = store.accounts_watching(conn, symbol)
    out = []
    for d in store.active_devices_for_accounts(conn, accounts):
        p = d.preferences
        if p.get("paused") or not p.get("watchlistPriority", True):
            continue
        if session not in (p.get("sessions") or ["regular"]):
            continue
        if _in_quiet_window(p, ts, d.time_zone):
            continue
        out.append(d)
    return out


def build_payload(cluster, text: str) -> dict:
    """APNs payload: title/body in `aps`, plus the ids the app needs to
    open the observation. Body is the plain-language headline the
    journal recorded; no numbers the record doesn't hold."""
    from tradebot.api.v1 import signal_uuid

    tier = getattr(cluster, "tier", "high")
    title = f"{cluster.symbol} — {'worth a closer look' if tier == 'high' else 'context'}"
    body = (getattr(cluster, "headlines", None) or text or "").split("\n")[0][:180]
    return {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default", "thread-id": cluster.symbol, "category": CATEGORY},
        "signalId": signal_uuid(cluster.id),
        "symbol": cluster.symbol,
        "observedAt": cluster.ts_utc,
    }


def make_push_subscriber_hook(users_conn):
    def hook(cluster, text: str, entry_mid=None) -> None:
        ts = datetime.fromisoformat(cluster.ts_utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        devices = eligible_devices(users_conn, cluster.symbol, ts)
        if not devices:
            return
        n = store.enqueue(users_conn, cluster.id, devices, build_payload(cluster, text), collapse_id=cluster.id, now=ts)
        logger.info("push_enqueued alert_id=%s devices=%d inserted=%d", cluster.id, len(devices), n)

    return hook


def compose_hooks(*hooks):
    """Runs each hook; a failure in one is logged and never stops the
    others -- Telegram delivery must not depend on push, or vice versa."""
    hooks = [h for h in hooks if h is not None]

    def composed(cluster, text: str, entry_mid=None) -> None:
        for h in hooks:
            try:
                h(cluster, text, entry_mid)
            except Exception:  # noqa: BLE001
                logger.exception("subscriber hook %s failed for alert_id=%s", getattr(h, "__name__", h), getattr(cluster, "id", "?"))

    return composed if hooks else None
