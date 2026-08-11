"""Golden-file tests for tradebot.rendering.templates — one function per
message type, snapshotted exactly so a formatting change shows up as a
diff here instead of surprising someone in Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.costs import Breakeven, ContractSelection, Leg
from tradebot.detectors import DailyAnchors
from tradebot.rendering import templates
from tradebot.journal import HistoricalPerformance, TierPerformance
from tradebot.marketdata import OptionContract, Quote


def _anchors() -> DailyAnchors:
    return DailyAnchors(
        symbol="GOOGL", session_date=date(2026, 7, 23), prior_close=377.68,
        prior_high=384.44, prior_low=379.50, opening_range_high=380.20,
        opening_range_low=378.90, opening_range_volume=250_000,
        swing_high=386.10, swing_low=365.00, avg_cum_volume_by_bar={},
    )


def _quote() -> Quote:
    return Quote(symbol="GOOGL", ts=datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc), bid=365.98, ask=366.02, last=366.00)


def _selection() -> ContractSelection:
    contract = OptionContract(
        symbol="GOOGL260814P00365000", expiry=date(2026, 8, 14), strike=365.0, right="put",
        bid=4.15, ask=4.25, last=4.20, delta=-0.47, theta=-0.35, open_interest=3412,
    )
    breakeven = Breakeven(pct=0.029, atr_units=2.9, legs=(Leg(contract, "long"),), is_vertical=False)
    return ContractSelection(
        breakeven=breakeven, no_trade=None, expiry=date(2026, 8, 14), dte=13,
        similar_setups_sample=60, insufficient_sample=False,
    )


def _history() -> HistoricalPerformance:
    return HistoricalPerformance(sample_size=20, continuation_rate=0.35, avg_return_pct=-0.62, offset_min=30)


def _cluster(**overrides) -> Cluster:
    fields = dict(
        id="fd153a2c-89ab-4c00-9e12-abc123456789",
        ts_utc="2026-07-23T16:05:00+00:00",
        session="2026-07-23",
        symbol="GOOGL",
        kinds="level_break,range_expansion,round_number_break",
        headlines="level break; range expansion; round number break",
        primary_headline=(
            "Broke below the $370 round number and took out the prior low "
            "on a range 15x its 14-day average."
        ),
        score=15.77,
        tier="high",
        close=366.00,
        atr14=1.77,
        trend="down",
        code_version="f665fba",
    )
    fields.update(overrides)
    return Cluster(**fields)


def test_render_high_alert_golden_matches_the_target_render_exactly():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history())
    assert text == (
        "<b>🔴 HIGH · GOOGL · BEARISH</b>\n"
        "\n"
        "Broke below the $370 round number and took out the prior low on a range 15x its 14-day average.\n"
        "\n"
        "<code>Signal strength     6.0 / 6\n"
        "Last                $366.00\n"
        "Prior close         $377.68\n"
        "Session             $379.50–$384.44\n"
        "ATR(14)             1.77\n"
        "Similar setups      20 historical observations\n"
        "30m follow-through  35.00%\n"
        "Contract            none tradable</code>\n"
        "\n"
        "level break · range expansion · round number\n"
        "<i>12:05 ET · fd153a · Not advice.</i>"
    )


def test_render_high_alert_signal_strength_caps_at_six_for_an_outlier_score():
    """Signal Strength is a display-only cap on the raw ATR score, not a
    change to real scoring/tiering — cluster.score itself (used for
    tier_for_score and the daily cap/cooldown logic) is untouched."""
    text = templates.render_high_alert(_cluster(score=15.77), _anchors(), _quote(), None, None)
    assert "Signal strength     6.0 / 6" in text

    text_uncapped = templates.render_high_alert(_cluster(score=4.7), _anchors(), _quote(), None, None)
    assert "Signal strength     4.7 / 6" in text_uncapped


def test_render_high_alert_body_is_under_12_visual_lines():
    # "visual lines" = distinct content lines a reader scans, not the
    # blank spacer lines between sections (the target render itself has
    # 3 of those) and not extra lines from Telegram client-side wrapping
    # of the rationale sentence (which the source never hard-wraps).
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history())
    content_lines = [line for line in text.split("\n") if line.strip()]
    assert len(content_lines) <= 12


def test_render_high_alert_has_exactly_one_emoji_the_tier_marker():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history())
    tier_emojis = {"🔴", "🟡", "⚪"}
    found = [ch for ch in text if ch in tier_emojis]
    assert found == ["🔴"]


def test_render_high_alert_has_no_exclamation_marks():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history())
    assert "!" not in text


def test_render_high_alert_shows_a_tradable_contract():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), _selection(), _history())
    assert "Contract            $365.00P 8/14 · BE +2.90% (2.90 ATR)" in text


def test_render_high_alert_never_omits_a_row_when_breakeven_and_history_are_none():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "Contract            none tradable" in text
    assert "Similar setups      —" in text
    assert "30m follow-through  —" in text


def test_render_high_alert_dashes_missing_atr_instead_of_omitting_the_row():
    text = templates.render_high_alert(_cluster(atr14=None), _anchors(), _quote(), None, None)
    assert "ATR(14)             —" in text


def test_render_high_alert_never_exposes_the_full_uuid():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "fd153a2c-89ab-4c00-9e12-abc123456789" not in text
    assert "fd153a" in text


def test_render_high_alert_humanizes_detector_kinds_on_the_tag_line():
    text = templates.render_high_alert(_cluster(kinds="vwap_break,rvol_spike"), _anchors(), _quote(), None, None)
    assert "VWAP break · volume spike" in text
    assert "vwap_break" not in text and "rvol_spike" not in text


def test_render_high_alert_news_driven_replaces_similar_setups_line():
    """Continuation stats are built on technical-setup history and don't
    transfer to an event-driven move — see tradebot.events module
    docstring. news_driven=True must override the Similar setups row
    (and drop the follow-through row entirely — there's nothing to
    report follow-through on) even though a (real) history sample was
    passed in."""
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history(), news_driven=True)
    assert "Similar setups   continuation stats do not apply" in text
    assert "35.00%" not in text  # the (contaminated) sample must not leak through
    assert "follow-through" not in text


def test_render_high_alert_shows_real_similar_setups_when_not_news_driven():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, _history(), news_driven=False)
    assert "Similar setups      20 historical observations" in text
    assert "30m follow-through  35.00%" in text
    assert "continuation stats do not apply" not in text


def test_render_digest_golden():
    tier_perf = TierPerformance(tier="medium", sample_size=45, continuation_rate=0.51, avg_return_pct=0.08, offset_min=30)
    clusters = [
        _cluster(id="b2", symbol="TSLA", kinds="vwap_break", tier="medium", score=2.1),
        _cluster(id="b3", symbol="AMD", kinds="rvol_spike", tier="medium", score=2.5),
    ]
    when = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
    text = templates.render_digest("Medium Digest", "medium", clusters, tier_perf, when)
    assert text == (
        "<b>🟡 Medium Digest</b> · 2 tickers\n"
        "<i>Track record: 51% cont. (n=45)</i>\n"
        "\n"
        "TSLA · VWAP break · 2.10 ATR\n"
        "AMD · volume spike · 2.50 ATR\n"
        "\n"
        "<i>11:00 ET · Not advice.</i>"
    )


def test_render_digest_omits_track_record_line_when_no_history():
    clusters = [_cluster(id="b2", symbol="TSLA", tier="medium")]
    text = templates.render_digest("Medium Digest", "medium", clusters, None, datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc))
    assert "Track record" not in text


def test_render_log_summary_golden():
    tier_perf = TierPerformance(tier="log", sample_size=200, continuation_rate=0.50, avg_return_pct=0.01, offset_min=30)
    clusters = [
        _cluster(id="c1", symbol="TSLA", tier="log"),
        _cluster(id="c2", symbol="TSLA", tier="log"),
        _cluster(id="c3", symbol="AMD", tier="log"),
    ]
    when = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
    text = templates.render_log_summary(clusters, tier_perf, when)
    assert text == (
        "<b>⚪ Log Summary</b> · 3 sub-threshold\n"
        "<i>Track record: 50% cont. (n=200)</i>\n"
        "\n"
        "TSLA: 2\n"
        "AMD: 1\n"
        "\n"
        "<i>16:00 ET · Not advice.</i>"
    )


def test_render_morning_briefing_golden():
    tier_perf = TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)
    when = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    text = templates.render_morning_briefing(tier_perf, when)
    assert text == (
        "<b>Morning Briefing</b>\n"
        "\n"
        "1. HIGH tier only — MEDIUM/LOG sit near a coin flip, not actionable.\n"
        "2. Act immediately — waiting for confirmation tested worse, not better.\n"
        "3. No proven best hours — trade HIGH whenever it fires, not on a schedule.\n"
        "4. Check the track record before acting — a low rate is a real reason to skip.\n"
        "5. Compare Score to the contract's breakeven — skip if the hurdle exceeds the typical move.\n"
        "6. Respect the daily cap and cooldown — they stop overtrading.\n"
        "\n"
        "<i>Current HIGH track record: 60% cont. (n=42)</i>\n"
        "\n"
        "<i>09:30 ET · Not advice.</i>"
    )


def test_render_morning_briefing_omits_track_record_line_on_an_empty_journal():
    text = templates.render_morning_briefing(None, datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc))
    assert "Current HIGH track record" not in text


def _event_window(**overrides):
    from tradebot.events import EventWindow

    fields = dict(
        id=1, symbol="TSLA", kind="earnings", start_utc=datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc), severity="downgrade",
        source="test", detail="after the close",
    )
    fields.update(overrides)
    return EventWindow(**fields)


def test_render_pre_open_card_golden_with_events():
    when = datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc)
    events = [
        _event_window(id=1, symbol="TSLA", kind="earnings", severity="downgrade", detail="after the close"),
        _event_window(id=2, symbol=None, kind="fomc", severity="suppress", detail=None),
        _event_window(id=3, symbol="GOOGL", kind="form4", severity="context", detail="routine Form 4"),
    ]
    text = templates.render_pre_open_card(events, date(2026, 7, 23), when)
    assert text == (
        "<b>Pre-Open — 2026-07-23</b>\n"
        "\n"
        "TSLA — earnings (downgrade) · after the close\n"
        "Market-wide — FOMC (blackout)\n"
        "GOOGL — Form 4 (context) · routine Form 4\n"
        "\n"
        "<i>09:00 ET · Not advice.</i>"
    )


def test_render_pre_open_card_says_no_known_events_rather_than_omitting_the_body():
    when = datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc)
    text = templates.render_pre_open_card([], date(2026, 7, 23), when)
    assert "No known earnings, macro, or filing events today." in text


def test_render_pre_open_card_escapes_and_never_exclaims():
    when = datetime(2026, 7, 23, 13, 0, tzinfo=timezone.utc)
    events = [_event_window(detail="<script>alert(1)</script>")]
    text = templates.render_pre_open_card(events, date(2026, 7, 23), when)
    assert "<script>" not in text
    assert "!" not in text


def test_render_heartbeat_golden():
    tier_perf = {"high": TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)}
    when = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
    text = templates.render_heartbeat(
        date(2026, 7, 23), timedelta(hours=6, minutes=30), {"high": 2, "medium": 5, "log": 12},
        {"cooldown_active": 3}, ["BE: no prior daily bar"], [], tier_perf, when,
    )
    assert text == (
        "<b>Heartbeat</b> · 2026-07-23\n"
        "\n"
        "<code>Uptime      6:30:00\n"
        "High        2\n"
        "Medium      5\n"
        "Log         12\n"
        "Suppressed  3\n"
        "Data gaps   1\n"
        "Errors      0</code>\n"
        "\n"
        "<i>HIGH: 60% cont. (n=42)</i>\n"
        "\n"
        "- BE: no prior daily bar\n"
        "\n"
        "<i>16:00 ET · Not advice.</i>"
    )


def test_render_heartbeat_truncates_data_gaps_past_five():
    gaps = [f"SYM{i}: no prior daily bar" for i in range(7)]
    text = templates.render_heartbeat(
        date(2026, 7, 23), timedelta(hours=1), {}, {}, gaps, [], None, datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc),
    )
    assert "...and 2 more" in text
    assert "SYM6" not in text  # only the first 5 are printed individually


def test_render_system_notice_golden():
    when = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)
    text = templates.render_system_notice(
        "Daily high-tier alert cap (8) reached. Suppressing further HIGH alerts today.", when,
    )
    assert text == (
        "<b>System</b>\n"
        "Daily high-tier alert cap (8) reached. Suppressing further HIGH alerts today.\n"
        "\n"
        "<i>15:00 ET · Not advice.</i>"
    )


def _position_size(**overrides):
    from tradebot.costs import PositionSize

    fields = dict(max_contracts=5, dollars_at_risk=1000.0, risk_budget=1000.0, exceeds_limit=False)
    fields.update(overrides)
    return PositionSize(**fields)


def test_render_position_size_golden_within_budget():
    when = datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)
    text = templates.render_position_size(_position_size(), when)
    assert text == (
        "<b>Position size</b>\n"
        "\n"
        "Max contracts: 5\n"
        "At risk: $1,000.00 (budget $1,000.00)\n"
        "\n"
        "<i>12:05 ET · Not advice.</i>"
    )


def test_render_position_size_golden_exceeds_limit():
    when = datetime(2026, 7, 23, 16, 5, tzinfo=timezone.utc)
    size = _position_size(max_contracts=0, dollars_at_risk=0.0, exceeds_limit=True)
    text = templates.render_position_size(size, when)
    assert text == (
        "<b>Position size</b>\n"
        "\n"
        "position exceeds your risk limit — skip.\n"
        "\n"
        "<i>12:05 ET · Not advice.</i>"
    )


def _contract_outcome(**overrides):
    from tradebot.journal import ContractOutcome

    fields = dict(
        symbol="META", right="call", strike=600.0, expiry="2026-04-17", entry_mid=2.96,
        mid_30m=3.24, mid_60m=None, mid_close=1.85, day_low=1.43, day_high=3.90,
    )
    fields.update(overrides)
    return ContractOutcome(**fields)


def test_render_contract_outcome_golden():
    when = datetime(2026, 4, 8, 20, 5, tzinfo=timezone.utc)
    text = templates.render_contract_outcome(_contract_outcome(), when)
    assert text == (
        "<b>Contract outcome</b>\n"
        "\n"
        "$600.00 Call exp 4/17\n"
        "Entry (at alert): $2.96\n"
        "At +30m: $3.24 (+9.5%, profitable)\n"
        "At close: $1.85 (-37.5%, not profitable)\n"
        "\n"
        "Day's range for this contract: $1.43 - $3.90\n"
        "Max theoretical profit that day: +172.7% (buy the low, sell the high)\n"
        "\n"
        "<i>16:05 ET · Not advice.</i>"
    )


def test_render_contract_outcome_with_no_checkpoints_yet():
    when = datetime(2026, 4, 8, 20, 5, tzinfo=timezone.utc)
    outcome = _contract_outcome(mid_30m=None, mid_60m=None, mid_close=None, day_low=None, day_high=None)
    text = templates.render_contract_outcome(outcome, when)
    assert "No forward prices recorded yet" in text
    assert "not available yet" in text.lower()
    assert "profitable" not in text.lower()


def test_render_contract_outcome_put_label():
    outcome = _contract_outcome(right="put")
    when = datetime(2026, 4, 8, 20, 5, tzinfo=timezone.utc)
    text = templates.render_contract_outcome(outcome, when)
    assert "Put exp" in text


def _track_record_for_pin(**overrides):
    from tradebot.telegram_bot.performance import SignificanceCheck, TrackRecord

    fields = dict(
        tier="high", offset_min=30, sample_size=466, hit_rate=0.4957, avg_return_pct=0.03,
        max_drawdown_pct=-27.10, longest_losing_streak=9, news_driven=None, clean_technical=None,
        total_alerts=10, total_no_trade=7, no_trade_tracked_count=7,
        significance=SignificanceCheck(z_score=-0.19, is_significant=False, n_needed_for_meaningful_edge=785),
    )
    fields.update(overrides)
    return TrackRecord(**fields)


def test_render_pinned_status_golden_not_significant():
    when = datetime(2026, 8, 6, 16, 5, tzinfo=timezone.utc)
    text = templates.render_pinned_status(_track_record_for_pin(), when)
    assert text == (
        "<b>BETA — live sample size</b>\n"
        "\n"
        "HIGH tier: 466 alerts so far (+30m).\n"
        "Not yet statistically different from a coin flip (z=-0.19). ~785 alerts needed to confirm even a modest real edge.\n"
        "\n"
        "Updated automatically each session — /performance has the full breakdown.\n"
        "\n"
        "<i>12:05 ET · Not advice.</i>"
    )


def test_render_pinned_status_golden_significant():
    from tradebot.telegram_bot.performance import SignificanceCheck

    when = datetime(2026, 8, 6, 16, 5, tzinfo=timezone.utc)
    tr = _track_record_for_pin(
        hit_rate=0.60, significance=SignificanceCheck(z_score=2.1, is_significant=True, n_needed_for_meaningful_edge=785),
    )
    text = templates.render_pinned_status(tr, when)
    assert "Statistically better than a coin flip (z=2.10) — still provisional." in text


def test_render_pinned_status_with_no_history_says_so():
    when = datetime(2026, 8, 6, 16, 5, tzinfo=timezone.utc)
    text = templates.render_pinned_status(None, when)
    assert "Not enough tracked history yet" in text
    assert "beta" in text.lower()


def _weekly_recap(**overrides):
    from tradebot.telegram_bot.performance import SignificanceCheck

    fields = dict(
        week_start="2026-07-27T00:00:00+00:00", week_end="2026-08-03T00:00:00+00:00", tier="high", offset_min=30,
        sample_size=20, hit_rate=0.6, avg_return_pct=0.35,
        significance=SignificanceCheck(z_score=0.89, is_significant=False, n_needed_for_meaningful_edge=785),
        total_alerts=20, total_no_trade=4, no_trade_tracked_count=20,
    )
    fields.update(overrides)

    @dataclass(frozen=True)
    class _Recap:
        week_start: str
        week_end: str
        tier: str
        offset_min: int
        sample_size: int
        hit_rate: float | None
        avg_return_pct: float | None
        significance: object
        total_alerts: int
        total_no_trade: int
        no_trade_tracked_count: int

    return _Recap(**fields)


def test_render_weekly_recap_golden_good_week():
    when = datetime(2026, 8, 3, 16, 5, tzinfo=timezone.utc)
    text = templates.render_weekly_recap(_weekly_recap(), when)
    assert text == (
        "<b>Weekly recap — 2026-07-27T00:00:00+00:00 to 2026-08-03T00:00:00+00:00</b>\n"
        "\n"
        "HIGH tier alerts published: 20\n"
        "NO TRADE (system said sit this one out): 4 of 20 tracked\n"
        "\n"
        "Hit rate: 60.00%   Avg move: +0.35% (n=20, +30m)\n"
        "That's not statistically different from a coin flip this week (z=0.89).\n"
        "\n"
        "<i>12:05 ET · Not advice.</i>"
    )


def test_render_weekly_recap_golden_bad_week_uses_the_identical_template():
    """The whole point: a losing week renders through the SAME function,
    same section order, same significance line — nothing about a bad
    week is structurally smaller or softer than a good one."""
    from tradebot.telegram_bot.performance import SignificanceCheck

    when = datetime(2026, 8, 3, 16, 5, tzinfo=timezone.utc)
    bad = _weekly_recap(
        hit_rate=0.2, avg_return_pct=-0.9,
        significance=SignificanceCheck(z_score=-2.68, is_significant=True, n_needed_for_meaningful_edge=785),
    )
    good = _weekly_recap()
    bad_text = templates.render_weekly_recap(bad, when)
    good_text = templates.render_weekly_recap(good, when)

    assert bad_text == (
        "<b>Weekly recap — 2026-07-27T00:00:00+00:00 to 2026-08-03T00:00:00+00:00</b>\n"
        "\n"
        "HIGH tier alerts published: 20\n"
        "NO TRADE (system said sit this one out): 4 of 20 tracked\n"
        "\n"
        "Hit rate: 20.00%   Avg move: -0.90% (n=20, +30m)\n"
        "That's statistically worse than a coin flip this week (z=-2.68).\n"
        "\n"
        "<i>12:05 ET · Not advice.</i>"
    )
    # same line count, same section order — no template branching on outcome
    assert len(bad_text.splitlines()) == len(good_text.splitlines())


def test_render_weekly_recap_with_too_few_alerts_says_so_rather_than_a_fabricated_rate():
    when = datetime(2026, 8, 3, 16, 5, tzinfo=timezone.utc)
    thin = _weekly_recap(
        sample_size=2, hit_rate=None, avg_return_pct=None, significance=None,
        total_alerts=2, no_trade_tracked_count=0, total_no_trade=0,
    )
    text = templates.render_weekly_recap(thin, when)
    assert "not enough tracked alerts" in text.lower()
    assert "hit rate:" not in text.lower()


def test_no_message_type_uses_financial_advice_wording_or_exclamation_marks():
    when = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)
    tier_perf = TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)
    messages = [
        templates.render_high_alert(_cluster(), _anchors(), _quote(), _selection(), _history()),
        templates.render_digest("Medium Digest", "medium", [_cluster(tier="medium")], tier_perf, when),
        templates.render_log_summary([_cluster(tier="log")], tier_perf, when),
        templates.render_morning_briefing(tier_perf, when),
        templates.render_heartbeat(date(2026, 7, 23), timedelta(hours=1), {}, {}, [], [], None, when),
        templates.render_system_notice("halt requested", when),
        templates.render_position_size(_position_size(), when),
        templates.render_position_size(_position_size(exceeds_limit=True), when),
        templates.render_weekly_recap(_weekly_recap(), when),
        templates.render_pinned_status(None, when),
        templates.render_contract_outcome(_contract_outcome(), when),
        templates.render_example(_real_win(), _day_hit_rate(), when),
    ]
    for text in messages:
        assert "!" not in text
        assert "financial advice" not in text.lower()
        assert text.rstrip().endswith("Not advice.</i>")


def test_all_interpolated_text_is_html_escaped():
    cluster = _cluster(symbol="<script>", primary_headline="a <b>bold</b> lie & a <tag>")
    text = templates.render_high_alert(cluster, _anchors(), _quote(), None, None)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert "&amp;" in text


def test_render_sample_alert_is_labeled_as_a_real_example_not_a_promise():
    text = templates.render_sample_alert()
    assert "META" in text
    assert "not a mockup" in text.lower()
    assert "+3.47%" in text
    assert "one real win, not the average" in text.lower()
    assert "/performance" in text


def _real_win(**overrides):
    from tradebot.telegram_bot.performance import RealWin

    fields = dict(
        detection_id="abc123", symbol="META", kinds="vwap_break", headline="META broke above VWAP (598.42), 0.77 ATR",
        trend="up", close=599.82, mark_price=620.62, return_pct=3.47, offset_min=30, ts_utc="2026-04-08T16:05:00+00:00",
    )
    fields.update(overrides)
    return RealWin(**fields)


def _day_hit_rate(**overrides):
    from tradebot.telegram_bot.performance import DayHitRate

    fields = dict(session="2026-06-17", hit_rate=0.5, sample_size=20, offset_min=30)
    fields.update(overrides)
    return DayHitRate(**fields)


def test_render_example_golden_both_present():
    when = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    text = templates.render_example(_real_win(), _day_hit_rate(), when)
    assert text == (
        "<b>One of the more notable real wins</b>\n"
        "\n"
        "META · VWAP break — bullish, calls favored\n"
        "META broke above VWAP (598.42), 0.77 ATR\n"
        "Entry ~$599.82 → +30m $620.62 (+3.47%)\n"
        "\n"
        "One real day's HIGH-tier hit rate — 2026-06-17: 50.00% (n=20)\n"
        "\n"
        "Real, but not typical — most real wins here are much smaller, and the overall record is "
        "a coin flip. /performance has the full, unfiltered picture.\n"
        "\n"
        "<i>16:00 ET · Not advice.</i>"
    )


def test_render_example_puts_favored_on_a_down_win():
    when = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    text = templates.render_example(_real_win(trend="down", symbol="USO"), _day_hit_rate(), when)
    assert "bearish, puts favored" in text


def test_render_example_says_so_when_no_real_win_exists_yet():
    when = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    text = templates.render_example(None, _day_hit_rate(), when)
    assert "no real win in the journal yet" in text.lower()
    assert "favored" not in text.lower()


def test_render_example_says_so_when_no_real_day_exists_yet():
    when = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    text = templates.render_example(_real_win(), None, when)
    assert "no real day with enough tracked alerts" in text.lower()


def test_render_example_never_fabricates_either_half():
    when = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    text = templates.render_example(None, None, when)
    assert "no real win" in text.lower()
    assert "no real day" in text.lower()
