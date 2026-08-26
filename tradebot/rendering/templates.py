"""Message templates — one function per message type, pure: data in,
string out. No formatting logic anywhere else in the codebase; every
number is composed from tradebot.rendering.fields.

Voice: Perch is calm and precise. The thesis is patience, and the copy
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
from datetime import date, datetime

from tradebot.guard import spread_pct_of_mid
from tradebot.rendering.fields import atr, dash, money, pct, qty, rate, ts

TIER_EMOJI = {"high": "🔴", "medium": "🟡", "log": "⚪"}
BIAS_LABEL = {"up": "BULLISH", "down": "BEARISH"}
DISCLAIMER = "Not advice."

# Detector kind -> the human label it gets on a tag line or digest row.
# Perch never shows a raw kind string (round_number_break, etc.) to a
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


# Perch never collapses these into one "no tradable contract" line —
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

# The alert shows Signal Strength — a raw ATR-based score, capped for
# display — and separately, Historical Follow-Through — a real measured
# rate. Never blended into one "confidence" number: the first is how
# unusual THIS bar was; the second is how similar setups actually played
# out. See tradebot.telegram_bot.performance.significance_check for the
# fuller statistical-significance verdict shown in /performance and
# /start, which a single alert has no room to restate — this cap only
# keeps an outlier score (a 15+ ATR move is real but rare) from reading
# as an arbitrarily large, made-up-looking number.
MAX_DISPLAY_SIGNAL_STRENGTH = 6.0

# Fallback label when there's no real history sample to attach an actual
# offset_min to (see _history_rows) — 30m is the convention every other
# follow-through stat in this project already uses by default (see
# journal.historical_performance, telegram_bot.performance.track_record).
DEFAULT_FOLLOWTHROUGH_OFFSET_MIN = 30


def _signal_strength(score: float) -> str:
    return f"{min(score, MAX_DISPLAY_SIGNAL_STRENGTH):.1f} / {MAX_DISPLAY_SIGNAL_STRENGTH:.0f}"


def _history_rows(history, news_driven: bool) -> list[tuple[str, str]]:
    """Two separate, always-present rows — Similar setups (a real sample
    size) and NNm follow-through (a real continuation rate) — never
    merged into one line, and never a stat built on a sample that doesn't
    apply. news_driven collapses both into the same override this alert
    has always shown for an event-driven move (see tradebot.events):
    there is no "follow-through" to report when the base rate itself
    doesn't transfer."""
    if news_driven:
        return [("Similar setups", NEWS_DRIVEN_SIMILAR_TEXT)]
    if history is None:
        return [
            ("Similar setups", "—"),
            (f"{DEFAULT_FOLLOWTHROUGH_OFFSET_MIN}m follow-through", "—"),
        ]
    return [
        ("Similar setups", f"{qty(history.sample_size)} historical observations"),
        (f"{history.offset_min}m follow-through", rate(history.continuation_rate * 100)),
    ]


def _extreme_mover_line(extreme_mover, quote) -> str:
    """Proposal 3's card prefix (docs/open-awareness-proposals-2026-08.md):
    states the evidence, not just the claim -- bar count and real volume,
    same as the guard actually checked. Spread is context, not a gate
    (Option B) -- always shown for a verified extreme mover so the
    reader sees exactly what the widened 15% ceiling let through, using
    spread_pct_of_mid so this can never disagree with what the guard
    itself computed."""
    spread = spread_pct_of_mid(quote)
    spread_note = f" · spread {rate(spread * 100)} — wide market" if spread is not None else ""
    return (
        f"<b>EXTREME MOVER</b> {rate(extreme_mover.gap_pct * 100)} vs prior close — "
        f"verified across 2 bars, {qty(extreme_mover.verified_volume)} shares{spread_note}"
    )


def render_high_alert(cluster, anchors, quote, selection, history, news_driven: bool = False, extreme_mover=None) -> str:
    """The single-ticker, full-detail HIGH alert. Fixed field order,
    every time: headline -> (extreme-mover prefix, if any) -> rationale ->
    stats block (signal strength, price context, real ATR, similar-setups
    follow-through, contract idea) -> tag line -> footer. Never tells the
    reader to trade — the contract row is an idea with a real breakeven
    or an explicit, reasoned NO TRADE, and the footer's "Not advice." is
    unconditional.

    `cluster.primary_headline` is the highest-scoring constituent
    detection's own headline — the rationale is that one sentence, not
    every trigger chained together (the full kind list is the tag line
    instead). `selection` is a costs.ContractSelection — never shown
    without its breakeven in ATR beside it (see _render_contract), and
    the two NO TRADE causes print differently, never collapsed into one.
    `news_driven`: this cluster overlaps a known event window (earnings,
    an EDGAR filing, a macro print) — see tradebot.events. Replaces the
    Similar Setups / follow-through rows rather than showing a technical
    base rate that doesn't apply.
    `extreme_mover`: a tradebot.guard.ExtremeMoverEvidence when this HIGH
    alert cleared guard.py's >25%-persistence check (Proposal 3) — None
    (default) for every ordinary alert, which renders identically to
    before this parameter existed.

    `cluster.origin == "screening"` (broad_scan promoted this symbol in
    for the session, it isn't on the subscriber's watchlist) gets a plain
    text "· RADAR" tag on the headline, not an emoji — SCANNER_PLAN.md's
    Alert format section is explicit that the tier marker is the only
    emoji this message ever carries. See
    docs/broad-scan-honesty-proposal.md's finding (a). The extreme-mover
    prefix follows the same rule — bold text, no emoji of its own.
    """
    tier_emoji = TIER_EMOJI.get(cluster.tier, "⚪")
    bias = BIAS_LABEL.get(cluster.trend, "NEUTRAL")
    symbol = html.escape(cluster.symbol)
    rationale = html.escape(cluster.primary_headline)
    is_screening = getattr(cluster, "origin", "watchlist") == "screening"

    radar_suffix = " · RADAR" if is_screening else ""
    headline = f"<b>{tier_emoji} {cluster.tier.upper()} · {symbol} · {bias}{radar_suffix}</b>"

    rows = [
        ("Signal strength", _signal_strength(cluster.score)),
        ("Last", money(quote.last)),
        ("Prior close", money(anchors.prior_close)),
        ("Session", f"{money(anchors.prior_low)}–{money(anchors.prior_high)}"),
        ("ATR(14)", dash(cluster.atr14, lambda v: f"{v:.2f}")),
        *_history_rows(history, news_driven),
        ("Contract", _render_contract(selection)),
    ]

    body = [headline]
    if extreme_mover is not None:
        body += ["", _extreme_mover_line(extreme_mover, quote)]
    body += ["", rationale]
    if is_screening:
        body += ["", "<i>RADAR: not on your watchlist — Perch's daily screen flagged it as active today.</i>"]
    body += ["", _stats_block(rows), "", _kind_tag(cluster.kinds)]

    when = datetime.fromisoformat(cluster.ts_utc)
    return "\n".join([*body, _footer(when, cluster.id)])


def render_digest(title: str, tier: str, clusters: list, tier_perf, when: datetime) -> str:
    """MEDIUM digest: one line per cluster. Batched into one message per
    hourly window by the caller (AlertBudget); the track record is
    stated once here, not repeated per ticker.

    Same plain-text "· RADAR" tag as render_high_alert for
    origin == "screening" clusters — see that function's docstring."""
    tier_emoji = TIER_EMOJI.get(tier, "⚪")
    header = f"<b>{tier_emoji} {html.escape(title)}</b> · {qty(len(clusters))} tickers"
    lines = [header]
    if tier_perf is not None:
        lines.append(f"<i>Track record: {_render_similar(tier_perf)}</i>")
    lines.append("")
    for c in clusters:
        radar_suffix = " · RADAR" if getattr(c, "origin", "watchlist") == "screening" else ""
        lines.append(f"{html.escape(c.symbol)} · {_kind_tag(c.kinds)} · {atr(c.score)}{radar_suffix}")
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
        "5. Compare Score to the contract's breakeven — skip if the hurdle exceeds the typical move.",
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
MAX_PRE_OPEN_EARNINGS_SYMBOLS_PER_TIMING = 40
MAX_PRE_OPEN_OTHER_EVENTS = 12


def _render_event_row(event) -> str:
    who = html.escape(event.symbol) if event.symbol else "Market-wide"
    kind_label = html.escape(EVENT_KIND_LABELS.get(event.kind, event.kind))
    severity_label = html.escape(EVENT_SEVERITY_LABELS.get(event.severity, event.severity))
    line = f"{who} — {kind_label} ({severity_label})"
    if event.detail:
        line += f" · {html.escape(event.detail)}"
    return line


def render_pre_open_card(
    events: list, session_date, when: datetime, *, earnings_coverage_error: bool = False,
) -> str:
    """Today's known earnings, macro prints, and filing-driven blackout
    windows, sent once before the alerting loop starts. Context, not a
    trade signal — no tier emoji. See tradebot.events module docstring:
    news is suppression and context, never an alert source, and this is
    the one place a day's events are all shown together rather than
    scattered across individual alert suppressions."""
    lines = [f"<b>Pre-Open — {html.escape(str(session_date))}</b>", ""]
    if earnings_coverage_error:
        lines.extend([
            "<b>Coverage incomplete: scheduled earnings calendar unavailable.</b>",
            "The scanner is continuing; do not read an absent earnings row as a confirmed quiet day.",
            "",
        ])
    if not events:
        if earnings_coverage_error:
            lines.append("No other known macro or filing events today.")
        else:
            lines.append("No known earnings, macro, or filing events today.")
    else:
        earnings = [event for event in events if event.kind == "earnings"]
        other_events = [event for event in events if event.kind != "earnings"]

        if earnings:
            lines.append(f"<b>Scheduled earnings context: {len(earnings)} active symbol(s)</b>")
            timing_groups = (
                ("Pre-market", [e.symbol for e in earnings if "(pre-market)" in (e.detail or "")]),
                ("After-hours", [e.symbol for e in earnings if "(after-hours)" in (e.detail or "")]),
                ("Timing unspecified", [e.symbol for e in earnings if "(unspecified)" in (e.detail or "")]),
            )
            for label, symbols in timing_groups:
                if not symbols:
                    continue
                ordered = sorted(set(symbols))
                shown = ordered[:MAX_PRE_OPEN_EARNINGS_SYMBOLS_PER_TIMING]
                suffix = (
                    f" (+{len(ordered) - len(shown)} more)" if len(shown) < len(ordered) else ""
                )
                lines.append(f"{label}: {html.escape(', '.join(shown))}{suffix}")

            # Manual/legacy rows may not carry the adapter's parenthesized
            # timing token. Preserve their exact symbol/detail instead of
            # letting the compact market-wide grouping erase them.
            ungrouped = [
                event for event in earnings
                if not any(
                    marker in (event.detail or "")
                    for marker in ("(pre-market)", "(after-hours)", "(unspecified)")
                )
            ]
            for event in ungrouped[:MAX_PRE_OPEN_OTHER_EVENTS]:
                lines.append(_render_event_row(event))
            if len(ungrouped) > MAX_PRE_OPEN_OTHER_EVENTS:
                lines.append(f"Other earnings rows: +{len(ungrouped) - MAX_PRE_OPEN_OTHER_EVENTS} more")

        for event in other_events[:MAX_PRE_OPEN_OTHER_EVENTS]:
            lines.append(_render_event_row(event))
        if len(other_events) > MAX_PRE_OPEN_OTHER_EVENTS:
            lines.append(f"Other known events: +{len(other_events) - MAX_PRE_OPEN_OTHER_EVENTS} more")
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_heartbeat(
    session_date, uptime, tier_counts: dict, suppression_counts: dict,
    data_gaps: list, errors: list, tier_perf: dict | None, when: datetime,
    cache_fetch_failed: list | None = None,
) -> str:
    """End-of-session status. tier_perf, if given, is a dict of tier ->
    HistoricalPerformance-shaped object (from journal.tier_performance()).

    cache_fetch_failed: symbols whose close-time intraday cache fetch
    failed (see runner._cache_todays_intraday_bars) -- 2026-08-12
    incident review: _alert_if_cache_fetch_failed only pages on TOTAL
    failure (0 of N), by design, so a partial miss (some symbols, not
    all) needs its own always-visible surface that doesn't depend on
    whether it happened to also drag marks_written below
    total_detections -- the heartbeat fires every session unconditionally,
    unlike either alert. None (replay's call site, which never runs the
    close-time fetch at all) renders identically to an empty list."""
    rows = [
        ("Uptime", str(uptime)),
        ("High", qty(tier_counts.get("high", 0))),
        ("Medium", qty(tier_counts.get("medium", 0))),
        ("Log", qty(tier_counts.get("log", 0))),
        ("Suppressed", qty(sum(suppression_counts.values()))),
        ("Data gaps", qty(len(data_gaps))),
        ("Errors", qty(len(errors))),
    ]
    if cache_fetch_failed:
        rows.append(("Cache fetch failed", qty(len(cache_fetch_failed))))
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
    if cache_fetch_failed:
        lines.append("")
        lines.append(html.escape(f"- Cache fetch failed: {', '.join(sorted(cache_fetch_failed))}"))
    lines.append("")
    lines.append(_footer(when))
    return "\n".join(lines)


def render_system_notice(text: str, when: datetime) -> str:
    """Operational notices (halt, stale data, cap reached, errors) — no
    tier emoji (these aren't trading signals), just a short bold line."""
    return f"<b>System</b>\n{html.escape(text)}\n\n{_footer(when)}"


def render_failure_notice(text: str, when: datetime) -> str:
    """2026-08-12 incident: the backfill-implausible alert fired,
    delivered correctly, and was still missed -- it sat visually
    indistinguishable from the routine heartbeat right next to it, both
    just plain text in the same channel. Reserved for notices that mean
    "something is actually broken, go look now" (backfill wrote nothing,
    the close-time cache fetch failed) as opposed to render_system_notice's
    broader "operational, FYI" bucket (halt, stale data, cap reached) --
    a single leading emoji, deliberately not used by any routine/
    informational message, so this is the one thing in the channel that
    should never blend in."""
    return f"<b>⚠️ ALERT</b>\n{html.escape(text)}\n\n{_footer(when)}"


def render_position_size(size, when: datetime) -> str:
    """A per-user follow-up sent alongside a HIGH alert (see
    tradebot.telegram_bot.delivery) — sizing depends on account_size and
    risk_per_trade_pct, which only exist per-subscriber, so this is never
    part of the shared alert render. Duck-typed on a costs.PositionSize-
    shaped object (max_contracts, dollars_at_risk, risk_budget,
    exceeds_limit), same convention as _render_contract's `selection`.
    States the dollar loss, not just the percentage — that's the number
    that actually deters."""
    if size.exceeds_limit:
        body = "position exceeds your risk limit — skip."
    else:
        body = (
            f"Max contracts: {qty(size.max_contracts)}\n"
            f"At risk: {money(size.dollars_at_risk)} (budget {money(size.risk_budget)})"
        )
    return f"<b>Position size</b>\n\n{body}\n\n{_footer(when)}"


def _outcome_checkpoint_line(label: str, mid: float | None, entry_mid: float) -> str | None:
    if mid is None:
        return None
    move_pct = (mid - entry_mid) / entry_mid * 100
    sign = "+" if move_pct >= 0 else ""
    verdict = "profitable" if mid > entry_mid else "not profitable"
    return f"At {label}: {money(mid)} ({sign}{move_pct:.1f}%, {verdict})"


def render_contract_outcome(outcome, when: datetime) -> str:
    """The "how did the option itself do" follow-up for a specific
    alert's recommended contract — separate from render_high_alert
    (which only shows the entry) and from a user's own /took /closed
    trade (which is their real, personal fill, not necessarily this
    exact contract or timing). Two distinct things, never blended into
    one number: our actual entry's outcome at each checkpoint (profitable
    or not, against OUR entry_mid), and the contract's own real day
    range (what anyone trading it that day could have captured at best —
    independent of when our alert fired). See
    journal.pending_contract_day_range_backfills for why verticals never
    get a day range here."""
    expiry = date.fromisoformat(outcome.expiry)
    right_label = "Call" if outcome.right == "call" else "Put"
    lines = [
        "<b>Contract outcome</b>",
        "",
        f"{money(outcome.strike)} {right_label} exp {expiry.month}/{expiry.day}",
        f"Entry (at alert): {money(outcome.entry_mid)}",
    ]
    for label, mid in (("+30m", outcome.mid_30m), ("+60m", outcome.mid_60m), ("close", outcome.mid_close)):
        line = _outcome_checkpoint_line(label, mid, outcome.entry_mid)
        if line is not None:
            lines.append(line)
    if all(mid is None for mid in (outcome.mid_30m, outcome.mid_60m, outcome.mid_close)):
        lines.append("No forward prices recorded yet for this contract.")

    lines.append("")
    if outcome.day_low is not None and outcome.day_high is not None:
        max_profit_pct = (outcome.day_high - outcome.day_low) / outcome.day_low * 100
        lines.append(f"Day's range for this contract: {money(outcome.day_low)} - {money(outcome.day_high)}")
        lines.append(f"Max theoretical profit that day: +{max_profit_pct:.1f}% (buy the low, sell the high)")
    else:
        lines.append("Day's range for this contract: not available yet.")

    lines += ["", _footer(when)]
    return "\n".join(lines)


def render_pinned_status(tr, when: datetime) -> str:
    """The pinned ops-channel message — see runner.maybe_update_pinned_status.
    tr is duck-typed on a tradebot.telegram_bot.performance.TrackRecord-
    shaped object (or None, below MIN_HISTORY_SAMPLE). Same significance
    math and wording as /start's onboarding text — one source of truth
    for "is this actually a real edge yet," not two copies that could
    quietly disagree."""
    lines = ["<b>BETA — live sample size</b>", ""]
    if tr is None:
        lines.append("Not enough tracked history yet for a real sample size.")
    else:
        lines.append(f"HIGH tier: {qty(tr.sample_size)} alerts so far (+{tr.offset_min}m).")
        sig = tr.significance
        if sig.is_significant:
            direction = "better than" if tr.hit_rate > 0.5 else "worse than"
            lines.append(f"Statistically {direction} a coin flip (z={sig.z_score:.2f}) — still provisional.")
        else:
            lines.append(
                f"Not yet statistically different from a coin flip (z={sig.z_score:.2f}). "
                f"~{qty(sig.n_needed_for_meaningful_edge)} alerts needed to confirm even a modest real edge."
            )
    lines += ["", "Updated automatically each session — /performance has the full breakdown.", "", _footer(when)]
    return "\n".join(lines)


def render_weekly_recap(recap, when: datetime) -> str:
    """One template, every week, win or lose — a bad week gets exactly
    the same layout and level of detail as a good one, never a
    shorter/softer version. recap is duck-typed on a
    tradebot.telegram_bot.performance.WeeklyRecap-shaped object."""
    lines = [
        f"<b>Weekly recap — {html.escape(recap.week_start)} to {html.escape(recap.week_end)}</b>",
        "",
        f"HIGH tier alerts published: {qty(recap.total_alerts)}",
    ]
    if recap.no_trade_tracked_count:
        lines.append(
            f"NO TRADE (system said sit this one out): {qty(recap.total_no_trade)} of "
            f"{qty(recap.no_trade_tracked_count)} tracked"
        )
    lines.append("")
    if recap.hit_rate is None:
        lines.append(f"Not enough tracked alerts this week (n={qty(recap.sample_size)}) for a real hit rate.")
    else:
        sign = "+" if recap.avg_return_pct >= 0 else ""
        lines.append(
            f"Hit rate: {rate(recap.hit_rate * 100)}   Avg move: {sign}{recap.avg_return_pct:.2f}% "
            f"(n={qty(recap.sample_size)}, +{recap.offset_min}m)"
        )
        sig = recap.significance
        direction = "better than" if recap.hit_rate > 0.5 else "worse than"
        verdict = f"statistically {direction} a coin flip" if sig.is_significant else "not statistically different from a coin flip"
        lines.append(f"That's {verdict} this week (z={sig.z_score:.2f}).")
    lines += ["", _footer(when)]
    return "\n".join(lines)


# A real HIGH alert from this project's own replay history — detection
# id 65f6292989794a0b, META, 2026-04-08, reconstructed exactly (real
# cached bars -> the same detectors.build_anchors() call the live system
# uses -> the real render_high_alert renderer) rather than hand-typed.
# Frozen as a constant rather than recomputed live: the underlying cached
# bar files are the only thing that could change it, and re-deriving it
# on every onboarding completion buys nothing but a cache-file dependency
# for a one-time "here's the format" example. See git history / the
# detection id above to re-derive or verify it.
_SAMPLE_ALERT_RENDER = (
    "<b>🔴 HIGH · META · BULLISH</b>\n"
    "\n"
    "META broke above VWAP (598.42), 0.77 ATR\n"
    "\n"
    "<code>Last         $599.82\n"
    "Prior close  $575.14\n"
    "Session      $564.77–$575.15\n"
    "Score        4.37 ATR\n"
    "ATR(14)      1.81\n"
    "Similar      50% cont. (n=20)\n"
    "Contract     none tradable</code>\n"
    "\n"
    "range expansion · VWAP break\n"
    "<i>12:05 ET · 65f629 · Not advice.</i>"
)


def render_sample_alert() -> str:
    """Sent once, right after onboarding finishes — the "here's what you
    actually get" moment. A REAL past HIGH alert (not a mockup), shown
    with its real outcome, explicitly labeled as one example rather than
    a promise: the whole rest of this bot's honesty (the coin-flip
    verdict in /start and /performance) would be undercut by a cherry-
    picked "typical" claim sitting right next to it."""
    return (
        "<b>What an alert actually looks like</b>\n"
        "\n"
        "A real HIGH alert from this system's history — not a mockup:\n"
        "\n"
        f"{_SAMPLE_ALERT_RENDER}\n"
        "\n"
        "What actually happened 30 minutes later: $620.62 (+3.47%).\n"
        "\n"
        "One real win, not the average — /performance has the full, unfiltered track record, "
        "losing stretches included."
    )


def render_example(win, day, when: datetime) -> str:
    """/example — one of the more notable real wins (see
    performance.random_real_win's docstring: restricted to a disclosed
    top slice of real outcomes, not a uniform sample — most real wins in
    this journal are under 1%) plus a real day's hit rate, freshly and
    randomly picked every call. Both halves are real records, not
    generated numbers — this renderer only ever formats what it's
    handed; either half can be None if the journal doesn't have one yet,
    stated plainly instead of skipped or faked. The "notable, not
    typical" framing is not optional decoration — it is the thing that
    keeps this honest instead of a cherry-picked highlight reel."""
    lines = ["<b>One of the more notable real wins</b>", ""]
    if win is None:
        lines.append("No real win in the journal yet to show.")
    else:
        right = "call" if win.trend == "up" else "put"
        bias = "bullish" if win.trend == "up" else "bearish"
        lines += [
            f"{html.escape(win.symbol)} · {_kind_tag(win.kinds)} — {bias}, {right}s favored",
            html.escape(win.headline),
            f"Entry ~{money(win.close)} → +{win.offset_min}m {money(win.mark_price)} (+{win.return_pct:.2f}%)",
        ]
    lines.append("")
    if day is None:
        lines.append("No real day with enough tracked alerts yet for a day's hit rate.")
    else:
        lines.append(
            f"One real day's HIGH-tier hit rate — {html.escape(day.session)}: "
            f"{rate(day.hit_rate * 100)} (n={qty(day.sample_size)})"
        )
    lines += [
        "",
        "Real, but not typical — most real wins here are much smaller, and the overall record is "
        "a coin flip. /performance has the full, unfiltered picture.",
        "",
        _footer(when),
    ]
    return "\n".join(lines)
