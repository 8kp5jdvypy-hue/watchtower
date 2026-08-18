"""Weekly recap rendering — Part B of docs/phase4-proof-engine-proposal.md.
Pure, data in, string out (same discipline as tradebot.rendering.templates:
no wall-clock reads, no I/O). One function builds the data, two render it
(markdown for X/Reddit, an HTML fragment for email/web) from the exact
same RecapData — the two formats can never disagree about what happened
this week, because neither one independently recomputes anything.

Deterministic and idempotent by construction: build_recap_data() takes a
closed [week_start, week_end) window over data that only ever gets
appended to (marks are written once, at their fixed offset, never
edited) — the same week, against the same databases, produces the same
RecapData every time. The renderers never read the clock; "generated"
context, if a caller wants to show one, is the caller's job, not baked
in here.

Voice (docs/phase4-proof-engine-proposal.md, "Voice rules, baked into
the templates, not left to the caller"): zero emoji -- SCANNER_PLAN.md's
one-emoji-per-message convention has no single alert here to anchor one
to, so this gets none. No superlatives generated from the data --
only the number and its significance verdict, exactly like
tradebot.rendering.templates.render_weekly_recap already does for the
Telegram version. A losing week renders through the exact same
function as a winning one -- nothing here branches on hit_rate to
change structure, only the wording of which side of 50% it landed on.
"""
from __future__ import annotations

import html as html_lib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradebot.telegram_bot.performance import (
    PublicAlertRow,
    TrackRecord,
    WeeklyRecap,
    public_alert_history,
    track_record,
    weekly_recap,
)

ET = ZoneInfo("America/New_York")
SITE_URL = "https://perchmarkets.com/record"


@dataclass(frozen=True)
class RecapData:
    week_start: str
    week_end: str
    tier: str
    offset_min: int
    alerts: list[PublicAlertRow]  # newest sent first, public_alert_history()'s own order
    week: WeeklyRecap
    running_total: TrackRecord | None


def build_recap_data(
    journal_conn: sqlite3.Connection,
    users_conn: sqlite3.Connection,
    week_start: str,
    week_end: str,
    tier: str = "high",
    offset_min: int = 30,
) -> RecapData:
    """[week_start, week_end) — week_end exclusive, same convention
    weekly_recap() itself uses; callers pass the next week's start
    date, not the last included day.

    alerted_only=True throughout, not a caller-chosen option: the
    public record is the alerted population, binding (owner decision,
    2026-08-18 — see the proposal doc's "finding that changes the
    design"). There is no "everything journaled" mode here the way
    track_record()'s own default still offers /performance."""
    alerts = public_alert_history(
        journal_conn, users_conn, tier=tier, offset_min=offset_min, since=week_start, until=week_end
    )
    week = weekly_recap(journal_conn, week_start, week_end, tier=tier, offset_min=offset_min, alerted_only=True)
    running_total = track_record(journal_conn, tier=tier, offset_min=offset_min, alerted_only=True)
    return RecapData(
        week_start=week_start, week_end=week_end, tier=tier, offset_min=offset_min,
        alerts=alerts, week=week, running_total=running_total,
    )


def _direction_word(trend: str) -> str:
    return "bullish" if trend == "up" else "bearish"


def _pct_signed(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_sent_at(iso: str) -> str:
    """Real outbox.delivered_at, in ET — the same timezone every other
    Perch surface (alert cards, the dashboard) already reports in.
    Avoids strftime's non-portable day-of-month flags (%-d / %#d)."""
    dt = datetime.fromisoformat(iso).astimezone(ET)
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%H:%M')} ET"


def _week_label(week_start: str, week_end: str) -> str:
    start = date.fromisoformat(week_start)
    end = date.fromisoformat(week_end) - timedelta(days=1)  # week_end is exclusive
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"


def _running_total_line(tr: TrackRecord | None, offset_min: int) -> str:
    """Plain text, no markup — both renderers call this and either use
    it verbatim (markdown) or html.escape() it (HTML), so the actual
    words can never diverge between the two formats."""
    if tr is None:
        return "Not enough tracked alerts yet (all-time) for a real hit rate."
    if tr.hit_rate > 0.5:
        direction = "better than"
    elif tr.hit_rate < 0.5:
        direction = "worse than"
    else:
        direction = "even with"
    sig = tr.significance
    verdict = (
        f"Statistically {direction} a coin flip (z={sig.z_score:.2f})."
        if sig.is_significant
        else f"Not yet statistically different from a coin flip (z={sig.z_score:.2f}) — still an early sample."
    )
    return (
        f"Running total (all HIGH alerts, all-time): n={tr.sample_size}, "
        f"hit rate {tr.hit_rate * 100:.1f}%, avg move {_pct_signed(tr.avg_return_pct)} "
        f"(+{offset_min}m). {verdict}"
    )


def render_recap_markdown(data: RecapData) -> str:
    n = len(data.alerts)
    lines = [
        f"**Perch — week of {_week_label(data.week_start, data.week_end)}**",
        "",
        f"{n} HIGH-tier alert{'s' if n != 1 else ''} sent this week.",
    ]
    for a in data.alerts:
        lines.append("")
        lines.append(f"{a.symbol} — {_direction_word(a.trend)} — {_fmt_sent_at(a.sent_at)}")
        lines.append(f'"{a.headline}"')
        outcome = _pct_signed(a.return_pct) if a.tracked else "pending"
        lines.append(f"+{data.offset_min}m: {outcome}")
    if n == 0:
        lines.append("")
        lines.append(f"No alerts sent this week (n=0) — see {SITE_URL} for the full history.")
    lines.append("")
    lines.append(_running_total_line(data.running_total, data.offset_min))
    lines.append("")
    lines.append(f"Sent, graded, unedited. — {SITE_URL}")
    return "\n".join(lines)


def render_recap_html(data: RecapData) -> str:
    n = len(data.alerts)
    parts = [
        f"<h2>Perch — week of {html_lib.escape(_week_label(data.week_start, data.week_end))}</h2>",
        f"<p>{n} HIGH-tier alert{'s' if n != 1 else ''} sent this week.</p>",
    ]
    if n == 0:
        parts.append(f'<p>No alerts sent this week — see <a href="{SITE_URL}">{SITE_URL}</a> for the full history.</p>')
    else:
        parts.append('<table class="recap-alerts">')
        parts.append(
            f"<thead><tr><th>Sent (ET)</th><th>Symbol</th><th>Direction</th>"
            f"<th>Headline</th><th>+{data.offset_min}m</th></tr></thead>"
        )
        parts.append("<tbody>")
        for a in data.alerts:
            outcome = _pct_signed(a.return_pct) if a.tracked else "pending"
            parts.append(
                "<tr>"
                f"<td>{html_lib.escape(_fmt_sent_at(a.sent_at))}</td>"
                f"<td>{html_lib.escape(a.symbol)}</td>"
                f"<td>{html_lib.escape(_direction_word(a.trend).upper())}</td>"
                f"<td>{html_lib.escape(a.headline)}</td>"
                f"<td>{html_lib.escape(outcome)}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    parts.append(f"<p>{html_lib.escape(_running_total_line(data.running_total, data.offset_min))}</p>")
    parts.append(f'<p><em>Sent, graded, unedited. — <a href="{SITE_URL}">{SITE_URL}</a></em></p>')
    return "\n".join(parts)
