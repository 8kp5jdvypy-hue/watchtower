"""M3 parity gate: per HIGH alert, did every Telegram delivery have an APNs
delivery within the window? Reads users.db (Telegram `outbox` and the
push `push_outbox`) -- see docs/telegram-sunset-plan-2026-09.md.

    python3 scripts/push_parity_report.py [--db data/users.db] [--sessions 10] [--window 60]

Prints one line per alert_id and a summary; exits 1 if any Telegram-
delivered alert has no APNs delivery, or any APNs delivery landed more
than --window seconds after the Telegram one. Only alerts that reached
at least one Telegram chat count (the ops channel is a chat too).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "users.db"))
    ap.add_argument("--sessions", type=int, default=10, help="distinct delivery days to examine, newest first")
    ap.add_argument("--window", type=float, default=60.0)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(delivered_at, 1, 10) FROM outbox WHERE status = 'delivered' AND alert_id NOT LIKE '%:sizing' "
        "ORDER BY 1 DESC LIMIT ?", (args.sessions,)
    )]
    if not days:
        print("no Telegram deliveries found"); return 1
    tg = conn.execute(
        "SELECT alert_id, MIN(delivered_at) FROM outbox WHERE status = 'delivered' AND alert_id NOT LIKE '%:sizing' "
        f"AND substr(delivered_at, 1, 10) IN ({','.join('?' * len(days))}) GROUP BY alert_id", days
    ).fetchall()
    push = {a: d for a, d in conn.execute(
        "SELECT alert_id, MIN(delivered_at) FROM push_outbox WHERE status = 'delivered' GROUP BY alert_id")}
    missing, late, ok = [], [], 0
    for alert_id, tg_at in tg:
        p_at = push.get(alert_id)
        if p_at is None:
            missing.append(alert_id); print(f"MISSING  {alert_id}  telegram={tg_at}"); continue
        lag = (datetime.fromisoformat(p_at) - datetime.fromisoformat(tg_at)).total_seconds()
        if lag > args.window:
            late.append(alert_id); print(f"LATE     {alert_id}  lag={lag:.0f}s")
        else:
            ok += 1; print(f"ok       {alert_id}  lag={lag:+.0f}s")
    print(f"\nsessions={len(days)} ({days[-1]}..{days[0]}) alerts={len(tg)} ok={ok} missing={len(missing)} late={len(late)}")
    return 0 if not missing and not late else 1


if __name__ == "__main__":
    sys.exit(main())
