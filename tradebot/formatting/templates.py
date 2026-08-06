"""Message templates — one function per message type, pure: data in,
string out. No formatting logic anywhere else in the codebase; every
number is composed from tradebot.formatting.fields.

Telegram HTML parse mode (not MarkdownV2 — fewer escaping bugs). All
interpolated text is html.escape()'d. Maximum one emoji per message, as
a tier indicator only (🔴 HIGH / 🟡 MEDIUM / ⚪ LOG). Bold the headline
only. No hard-wrapped prose — only structural line breaks between
semantically distinct fields/rows.
"""
from __future__ import annotations

import html
from datetime import datetime

from tradebot.formatting.fields import atr, money, pct, qty, ts

TIER_EMOJI = {"high": "🔴", "medium": "🟡", "log": "⚪"}
BIAS_TEXT = {"up": ("BULLISH", "calls"), "down": ("BEARISH", "puts")}
DISCLAIMER = "Not financial advice."


def _stats_block(rows: list[tuple[str, str]]) -> str:
    """Aligned two-column <code> block: label left, value right, padded
    to the longest label so columns line up in a monospace font."""
    escaped = [(html.escape(label), html.escape(value)) for label, value in rows]
    width = max(len(label) for label, _ in escaped) + 2
    lines = [f"{label:<{width}}{value}" for label, value in escaped]
    return "<code>" + "\n".join(lines) + "</code>"


def _footer(when: datetime, short_id: str | None = None) -> str:
    parts = [ts(when)]
    if short_id:
        parts.append(short_id[:6])
    parts.append(DISCLAIMER)
    return "<i>" + " · ".join(html.escape(p) for p in parts) + "</i>"


def _dash_if_none(value: float | None, formatter) -> str:
    return "—" if value is None else formatter(value)


def _render_breakeven(breakeven) -> str:
    if breakeven is None:
        return "no tradable contract"
    return f"{pct(breakeven.pct * 100)} ({atr(breakeven.atr_units)})"


def _render_history(history) -> str:
    if history is None:
        return "no track record yet"
    return f"{pct(history.continuation_rate * 100)} continued (n={qty(history.sample_size)}), {pct(history.avg_return_pct)} avg"


def render_high_alert(cluster, anchors, quote, breakeven, history) -> str:
    """The single-ticker, full-detail HIGH alert. Fixed field order:
    headline -> direction -> one-sentence rationale -> stats block ->
    footer, every time, no exceptions.

    `cluster.primary_headline` is the highest-scoring constituent
    detection's own headline — the rationale is that one sentence, not
    every trigger chained together (the full kind list is in the
    direction line's tag instead).
    """
    tier_emoji = TIER_EMOJI.get(cluster.tier, "⚪")
    bias_label, _option_side = BIAS_TEXT.get(cluster.trend, ("NEUTRAL", "either side"))
    kinds_tag = html.escape(", ".join(cluster.kinds.split(",")))
    symbol = html.escape(cluster.symbol)
    rationale = html.escape(cluster.primary_headline)

    headline = f"<b>{tier_emoji} {cluster.tier.upper()} {symbol}</b>"
    direction_line = f"{html.escape(bias_label)} · {kinds_tag}"

    rows = [
        ("Score", atr(cluster.score)),
        ("Close", money(cluster.close)),
        ("ATR14", _dash_if_none(cluster.atr14, atr)),
        ("Breakeven", _render_breakeven(breakeven)),
        ("Track Record", _render_history(history)),
        ("Range", f"{money(anchors.opening_range_low)}–{money(anchors.opening_range_high)}"),
        ("Prior Close", money(anchors.prior_close)),
        ("Quote", f"{money(quote.bid)} / {money(quote.ask)}"),
    ]

    when = datetime.fromisoformat(cluster.ts_utc)
    return "\n".join(
        [headline, direction_line, rationale, "", _stats_block(rows), "", _footer(when, cluster.id)]
    )


def render_digest(title: str, tier: str, clusters: list, tier_perf, when: datetime) -> str:
    """MEDIUM digest: one line per cluster. Batched into one message per
    hourly window by the caller (AlertBudget); the track record is
    stated once here, not repeated per ticker."""
    tier_emoji = TIER_EMOJI.get(tier, "⚪")
    header = f"<b>{tier_emoji} {html.escape(title)}</b> · {qty(len(clusters))} tickers"
    lines = [header]
    if tier_perf is not None:
        lines.append(f"<i>Track record: {_render_history(tier_perf)}</i>")
    lines.append("")
    for c in clusters:
        kinds = html.escape(", ".join(c.kinds.split(",")))
        lines.append(f"{html.escape(c.symbol)} · {kinds} · {atr(c.score)}")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_log_summary(clusters: list, tier_perf, when: datetime) -> str:
    """LOG summary: one line per SYMBOL with a count, not one line per
    cluster — a day can have dozens of sub-threshold detections, and
    listing each individually would blow the line budget."""
    tier_emoji = TIER_EMOJI.get("log", "⚪")
    header = f"<b>{tier_emoji} Log Summary</b> · {qty(len(clusters))} sub-threshold"
    lines = [header]
    if tier_perf is not None:
        lines.append(f"<i>Track record: {_render_history(tier_perf)}</i>")
    lines.append("")
    counts: dict[str, int] = {}
    for c in clusters:
        counts[c.symbol] = counts.get(c.symbol, 0) + 1
    for symbol, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"{html.escape(symbol)}: {qty(count)}")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_morning_briefing(tier_perf, when: datetime) -> str:
    """Compact numbered list, one line each — no sub-indented
    continuation lines. Rules here are grounded in what's actually been
    tested against this project's own data (see SCANNER_PLAN.md): HIGH
    tier only, no confirmation delay, no time-of-day filter."""
    lines = [
        "<b>🌅 Morning Briefing</b>",
        "",
        "1. HIGH tier only — MEDIUM/LOG sit near a coin flip, not actionable.",
        "2. Act immediately — waiting for confirmation tested worse, not better.",
        "3. No proven best hours — trade HIGH whenever it fires, not on a schedule.",
        "4. Check Track Record before acting — a low rate is a real reason to skip.",
        "5. Compare Score to Breakeven — skip if the hurdle exceeds typical delivery.",
        "6. Respect the daily cap and cooldown — they stop overtrading.",
    ]
    if tier_perf is not None:
        lines.append("")
        lines.append(f"<i>Current HIGH track record: {_render_history(tier_perf)}</i>")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_heartbeat(
    session_date, uptime, tier_counts: dict, suppression_counts: dict,
    data_gaps: list, errors: list, tier_perf: dict | None, when: datetime,
) -> str:
    """End-of-session status. tier_perf, if given, is a dict of tier ->
    HistoricalPerformance-shaped object (from journal.tier_performance())."""
    rows = [
        ("Uptime", str(uptime)),
        ("High", qty(tier_counts.get("high", 0))),
        ("Medium", qty(tier_counts.get("medium", 0))),
        ("Log", qty(tier_counts.get("log", 0))),
        ("Suppressed", qty(sum(suppression_counts.values()))),
        ("Data gaps", qty(len(data_gaps))),
        ("Errors", qty(len(errors))),
    ]
    lines = [f"<b>💓 Heartbeat</b> · {html.escape(str(session_date))}", "", _stats_block(rows)]
    if tier_perf:
        lines.append("")
        order = {"high": 0, "medium": 1, "log": 2}
        for tier in sorted(tier_perf, key=lambda t: order.get(t, 99)):
            lines.append(f"<i>{tier.upper()}: {_render_history(tier_perf[tier])}</i>")
    if data_gaps:
        lines.append("")
        for gap_note in data_gaps[:5]:
            lines.append(html.escape(f"- {gap_note}"))
        if len(data_gaps) > 5:
            lines.append(f"...and {qty(len(data_gaps) - 5)} more")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_system_notice(text: str, when: datetime) -> str:
    """Operational notices (halt, stale data, cap reached, errors) — no
    tier emoji (these aren't trading signals), just a short bold line."""
    return f"<b>⚠️ System</b>\n{html.escape(text)}\n\n{_footer(when)}"
