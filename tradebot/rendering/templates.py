"""Message templates — one function per message type, pure: data in,
string out. No formatting logic anywhere else in the codebase; every
number is composed from tradebot.rendering.fields.

Voice: Kestrel is calm and precise. The thesis is patience, and the copy
has to sound like it — no rockets, no urgency emoji, no exclamation
marks. Telegram HTML parse mode (not MarkdownV2 — fewer escaping bugs).
All interpolated text is html.escape()'d. Exactly one emoji per message,
the tier marker (🔴 HIGH / 🟡 MEDIUM / ⚪ LOG) — every other emoji that
used to live here (per-field icons, section headers) has been deleted.
No hard-wrapped prose in source: Telegram wraps the client side, so
rationale text is written as one unbroken sentence.
"""
from __future__ import annotations

import html
from datetime import datetime

from tradebot.rendering.fields import atr, dash, money, pct, qty, ts

TIER_EMOJI = {"high": "🔴", "medium": "🟡", "log": "⚪"}
BIAS_LABEL = {"up": "BULLISH", "down": "BEARISH"}
DISCLAIMER = "Not advice."

# Detector kind -> the human label it gets on a tag line or digest row.
# Kestrel never shows a raw kind string (round_number_break, etc.) to a
# reader.
KIND_LABELS = {
    "level_break": "level break",
    "range_expansion": "range expansion",
    "round_number_break": "round number",
    "vwap_break": "VWAP break",
    "gap": "gap",
    "rvol_spike": "volume spike",
}


def _kind_tag(kinds: str) -> str:
    return " · ".join(html.escape(KIND_LABELS.get(k, k)) for k in kinds.split(","))


def _stats_block(rows: list[tuple[str, str]]) -> str:
    """Aligned two-column <code> block: label left, value right, padded
    to the longest label so columns line up in a monospace font. A row
    is never omitted for missing data — see fields.dash."""
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


# Kestrel never collapses these into one "no tradable contract" line —
# they're different failures and a reader needs to know which one:
# a liquidity problem says nothing about whether the trade idea is any
# good, but a breakeven that exceeds the typical move says the idea may
# be fine and the options market just isn't offering a way to play it
# profitably at this size.
NO_TRADE_LABELS = {
    "no_liquid_strike": "no liquid strike",
    "breakeven_exceeds_typical_move": "breakeven exceeds typical move",
    "earnings_blackout": "earnings blackout",
}


def _render_leg(contract) -> str:
    side = "C" if contract.right == "call" else "P"
    return f"{money(contract.strike)}{side}"


def _render_contract(selection) -> str:
    """"none tradable" is a real, checked answer, not missing data — it
    does not get fields.dash's em-dash treatment. Never shows a contract
    without its breakeven in ATR beside it."""
    if selection is None or not selection.is_tradable:
        reason = NO_TRADE_LABELS.get(selection.no_trade.reason, selection.no_trade.detail) if selection and selection.no_trade else "none tradable"
        return f"none tradable — {reason}" if selection and selection.no_trade else "none tradable"

    be = selection.breakeven
    expiry = f"{selection.expiry.month}/{selection.expiry.day}"
    if be.is_vertical:
        long_c, short_c = be.legs[0].contract, be.legs[1].contract
        legs = f"Long {_render_leg(long_c)} / Short {_render_leg(short_c)}"
    else:
        legs = _render_leg(be.contract)
    line = f"{legs} {expiry} · BE {pct(be.pct * 100)} ({atr(be.atr_units)})"
    if selection.insufficient_sample:
        line += " · insufficient sample"
    return line


def _render_similar(history) -> str:
    return f"{history.continuation_rate * 100:.0f}% cont. (n={qty(history.sample_size)})"


NEWS_DRIVEN_SIMILAR_TEXT = "continuation stats do not apply"


def _render_similar_row(history, news_driven: bool) -> str:
    """continuation stats are built on technical-setup history and don't
    transfer to an event-driven move — see tradebot.events module
    docstring. A news-driven alert always gets the override, even if a
    (contaminated) history sample happens to exist for it."""
    if news_driven:
        return NEWS_DRIVEN_SIMILAR_TEXT
    return dash(history, _render_similar)


def render_high_alert(cluster, anchors, quote, selection, history, news_driven: bool = False) -> str:
    """The single-ticker, full-detail HIGH alert. Fixed field order,
    every time: headline -> rationale -> stats block -> tag line ->
    footer.

    `cluster.primary_headline` is the highest-scoring constituent
    detection's own headline — the rationale is that one sentence, not
    every trigger chained together (the full kind list is the tag line
    instead). `selection` is a costs.ContractSelection — never shown
    without its breakeven in ATR beside it (see _render_contract), and
    the two NO TRADE causes print differently, never collapsed into one.
    `news_driven`: this cluster overlaps a known event window (earnings,
    an EDGAR filing, a macro print) — see tradebot.events. Replaces the
    Similar Setups line rather than showing a technical base rate that
    doesn't apply.
    """
    tier_emoji = TIER_EMOJI.get(cluster.tier, "⚪")
    bias = BIAS_LABEL.get(cluster.trend, "NEUTRAL")
    symbol = html.escape(cluster.symbol)
    rationale = html.escape(cluster.primary_headline)

    headline = f"<b>{tier_emoji} {cluster.tier.upper()} · {symbol} · {bias}</b>"

    rows = [
        ("Last", money(quote.last)),
        ("Prior close", money(anchors.prior_close)),
        ("Session", f"{money(anchors.prior_low)}–{money(anchors.prior_high)}"),
        ("Score", atr(cluster.score)),
        ("ATR(14)", dash(cluster.atr14, lambda v: f"{v:.2f}")),
        ("Similar", _render_similar_row(history, news_driven)),
        ("Contract", _render_contract(selection)),
    ]

    when = datetime.fromisoformat(cluster.ts_utc)
    return "\n".join([
        headline, "", rationale, "", _stats_block(rows), "",
        _kind_tag(cluster.kinds), _footer(when, cluster.id),
    ])


def render_digest(title: str, tier: str, clusters: list, tier_perf, when: datetime) -> str:
    """MEDIUM digest: one line per cluster. Batched into one message per
    hourly window by the caller (AlertBudget); the track record is
    stated once here, not repeated per ticker."""
    tier_emoji = TIER_EMOJI.get(tier, "⚪")
    header = f"<b>{tier_emoji} {html.escape(title)}</b> · {qty(len(clusters))} tickers"
    lines = [header]
    if tier_perf is not None:
        lines.append(f"<i>Track record: {_render_similar(tier_perf)}</i>")
    lines.append("")
    for c in clusters:
        lines.append(f"{html.escape(c.symbol)} · {_kind_tag(c.kinds)} · {atr(c.score)}")
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
        lines.append(f"<i>Track record: {_render_similar(tier_perf)}</i>")
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
        "<b>Morning Briefing</b>",
        "",
        "1. HIGH tier only — MEDIUM/LOG sit near a coin flip, not actionable.",
        "2. Act immediately — waiting for confirmation tested worse, not better.",
        "3. No proven best hours — trade HIGH whenever it fires, not on a schedule.",
        "4. Check the track record before acting — a low rate is a real reason to skip.",
        "5. Compare Score to the contract's breakeven — skip if the hurdle exceeds typical delivery.",
        "6. Respect the daily cap and cooldown — they stop overtrading.",
    ]
    if tier_perf is not None:
        lines.append("")
        lines.append(f"<i>Current HIGH track record: {_render_similar(tier_perf)}</i>")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


# Event-window kind -> the human label it gets on the pre-open card / /events
# — a reader shouldn't have to know EDGAR's form codes or this project's
# internal event kind strings (see tradebot.events).
EVENT_KIND_LABELS = {
    "8-K": "8-K filing",
    "13D": "13D filing",
    "13G": "13G filing",
    "form4": "Form 4",
    "earnings": "earnings",
    "eia": "EIA petroleum status report",
    "fomc": "FOMC",
    "cpi": "CPI",
    "nfp": "NFP",
}
EVENT_SEVERITY_LABELS = {"suppress": "blackout", "downgrade": "downgrade", "context": "context"}


def _render_event_row(event) -> str:
    who = html.escape(event.symbol) if event.symbol else "Market-wide"
    kind_label = html.escape(EVENT_KIND_LABELS.get(event.kind, event.kind))
    severity_label = html.escape(EVENT_SEVERITY_LABELS.get(event.severity, event.severity))
    line = f"{who} — {kind_label} ({severity_label})"
    if event.detail:
        line += f" · {html.escape(event.detail)}"
    return line


def render_pre_open_card(events: list, session_date, when: datetime) -> str:
    """Today's known earnings, macro prints, and filing-driven blackout
    windows, sent once before the alerting loop starts. Context, not a
    trade signal — no tier emoji. See tradebot.events module docstring:
    news is suppression and context, never an alert source, and this is
    the one place a day's events are all shown together rather than
    scattered across individual alert suppressions."""
    lines = [f"<b>Pre-Open — {html.escape(str(session_date))}</b>", ""]
    if not events:
        lines.append("No known earnings, macro, or filing events today.")
    else:
        for event in events:
            lines.append(_render_event_row(event))
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
    lines = [f"<b>Heartbeat</b> · {html.escape(str(session_date))}", "", _stats_block(rows)]
    if tier_perf:
        lines.append("")
        order = {"high": 0, "medium": 1, "log": 2}
        for tier in sorted(tier_perf, key=lambda t: order.get(t, 99)):
            lines.append(f"<i>{tier.upper()}: {_render_similar(tier_perf[tier])}</i>")
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
    return f"<b>System</b>\n{html.escape(text)}\n\n{_footer(when)}"
