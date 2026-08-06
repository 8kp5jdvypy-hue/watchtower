"""Golden-file tests for tradebot.rendering.templates — one function per
message type, snapshotted exactly so a formatting change shows up as a
diff here instead of surprising someone in Telegram.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.costs import Breakeven
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


def _breakeven() -> Breakeven:
    contract = OptionContract(
        symbol="GOOGL260814P00365000", expiry=date(2026, 8, 14), strike=365.0, right="put",
        bid=4.15, ask=4.25, last=4.20, delta=-0.47, theta=-0.35, open_interest=3412,
    )
    return Breakeven(pct=0.029, atr_units=2.9, contract=contract)


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
        "<code>Last         $366.00\n"
        "Prior close  $377.68\n"
        "Session      $379.50–$384.44\n"
        "Score        15.77 ATR\n"
        "ATR(14)      1.77\n"
        "Similar      35% cont. (n=20)\n"
        "Contract     none tradable</code>\n"
        "\n"
        "level break · range expansion · round number\n"
        "<i>12:05 ET · fd153a · Not advice.</i>"
    )


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
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), _breakeven(), _history())
    assert "Contract     $365.00P 8/14 · BE +2.90%" in text


def test_render_high_alert_never_omits_a_row_when_breakeven_and_history_are_none():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "Contract     none tradable" in text
    assert "Similar      —" in text


def test_render_high_alert_dashes_missing_atr_instead_of_omitting_the_row():
    text = templates.render_high_alert(_cluster(atr14=None), _anchors(), _quote(), None, None)
    assert "ATR(14)      —" in text


def test_render_high_alert_never_exposes_the_full_uuid():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "fd153a2c-89ab-4c00-9e12-abc123456789" not in text
    assert "fd153a" in text


def test_render_high_alert_humanizes_detector_kinds_on_the_tag_line():
    text = templates.render_high_alert(_cluster(kinds="vwap_break,rvol_spike"), _anchors(), _quote(), None, None)
    assert "VWAP break · volume spike" in text
    assert "vwap_break" not in text and "rvol_spike" not in text


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
        "5. Compare Score to the contract's breakeven — skip if the hurdle exceeds typical delivery.\n"
        "6. Respect the daily cap and cooldown — they stop overtrading.\n"
        "\n"
        "<i>Current HIGH track record: 60% cont. (n=42)</i>\n"
        "\n"
        "<i>09:30 ET · Not advice.</i>"
    )


def test_render_morning_briefing_omits_track_record_line_on_an_empty_journal():
    text = templates.render_morning_briefing(None, datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc))
    assert "Current HIGH track record" not in text


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


def test_no_message_type_uses_financial_advice_wording_or_exclamation_marks():
    when = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)
    tier_perf = TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)
    messages = [
        templates.render_high_alert(_cluster(), _anchors(), _quote(), _breakeven(), _history()),
        templates.render_digest("Medium Digest", "medium", [_cluster(tier="medium")], tier_perf, when),
        templates.render_log_summary([_cluster(tier="log")], tier_perf, when),
        templates.render_morning_briefing(tier_perf, when),
        templates.render_heartbeat(date(2026, 7, 23), timedelta(hours=1), {}, {}, [], [], None, when),
        templates.render_system_notice("halt requested", when),
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
