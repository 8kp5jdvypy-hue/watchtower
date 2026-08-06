"""Golden-file tests for tradebot.formatting.templates — one function per
message type, snapshotted exactly so a formatting change shows up as a
diff here instead of surprising someone in Telegram.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradebot.alerts import Cluster
from tradebot.costs import Breakeven
from tradebot.detectors import DailyAnchors
from tradebot.formatting import templates
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


def test_render_high_alert_golden():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), _breakeven(), _history())
    assert text == (
        "<b>🔴 HIGH GOOGL</b>\n"
        "BEARISH · level_break, range_expansion, round_number_break\n"
        "Broke below the $370 round number and took out the prior low on a range 15x its 14-day average.\n"
        "\n"
        "<code>Score         15.77 ATR\n"
        "Close         $366.00\n"
        "ATR14         1.77 ATR\n"
        "Breakeven     +2.90% (2.90 ATR)\n"
        "Track Record  +35.00% continued (n=20), -0.62% avg\n"
        "Range         $378.90–$380.20\n"
        "Prior Close   $377.68\n"
        "Quote         $365.98 / $366.02</code>\n"
        "\n"
        "<i>2026-07-23 12:05 ET · fd153a · Not financial advice.</i>"
    )


def test_render_high_alert_never_omits_a_row_when_breakeven_and_history_are_none():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "Breakeven     no tradable contract" in text
    assert "Track Record  no track record yet" in text


def test_render_high_alert_dashes_missing_atr_instead_of_omitting_the_row():
    text = templates.render_high_alert(_cluster(atr14=None), _anchors(), _quote(), None, None)
    assert "ATR14         —" in text


def test_render_high_alert_never_exposes_the_full_uuid():
    text = templates.render_high_alert(_cluster(), _anchors(), _quote(), None, None)
    assert "fd153a2c-89ab-4c00-9e12-abc123456789" not in text
    assert "fd153a" in text


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
        "<i>Track record: +51.00% continued (n=45), +0.08% avg</i>\n"
        "\n"
        "TSLA · vwap_break · 2.10 ATR\n"
        "AMD · rvol_spike · 2.50 ATR\n"
        "\n"
        "<i>2026-07-23 11:00 ET · Not financial advice.</i>"
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
        "<i>Track record: +50.00% continued (n=200), +0.01% avg</i>\n"
        "\n"
        "TSLA: 2\n"
        "AMD: 1\n"
        "\n"
        "<i>2026-07-23 16:00 ET · Not financial advice.</i>"
    )


def test_render_morning_briefing_golden():
    tier_perf = TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)
    when = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
    text = templates.render_morning_briefing(tier_perf, when)
    assert text == (
        "<b>🌅 Morning Briefing</b>\n"
        "\n"
        "1. HIGH tier only — MEDIUM/LOG sit near a coin flip, not actionable.\n"
        "2. Act immediately — waiting for confirmation tested worse, not better.\n"
        "3. No proven best hours — trade HIGH whenever it fires, not on a schedule.\n"
        "4. Check Track Record before acting — a low rate is a real reason to skip.\n"
        "5. Compare Score to Breakeven — skip if the hurdle exceeds typical delivery.\n"
        "6. Respect the daily cap and cooldown — they stop overtrading.\n"
        "\n"
        "<i>Current HIGH track record: +59.50% continued (n=42), +0.36% avg</i>\n"
        "\n"
        "<i>2026-07-23 09:30 ET · Not financial advice.</i>"
    )


def test_render_morning_briefing_omits_track_record_line_on_an_empty_journal():
    text = templates.render_morning_briefing(None, datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc))
    assert "Current HIGH track record" not in text
    assert "no line to fabricate" not in text  # sanity: no stray placeholder text


def test_render_heartbeat_golden():
    tier_perf = {"high": TierPerformance(tier="high", sample_size=42, continuation_rate=0.595, avg_return_pct=0.356, offset_min=30)}
    when = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
    text = templates.render_heartbeat(
        date(2026, 7, 23), timedelta(hours=6, minutes=30), {"high": 2, "medium": 5, "log": 12},
        {"cooldown_active": 3}, ["BE: no prior daily bar"], [], tier_perf, when,
    )
    assert text == (
        "<b>💓 Heartbeat</b> · 2026-07-23\n"
        "\n"
        "<code>Uptime      6:30:00\n"
        "High        2\n"
        "Medium      5\n"
        "Log         12\n"
        "Suppressed  3\n"
        "Data gaps   1\n"
        "Errors      0</code>\n"
        "\n"
        "<i>HIGH: +59.50% continued (n=42), +0.36% avg</i>\n"
        "\n"
        "- BE: no prior daily bar\n"
        "\n"
        "<i>2026-07-23 16:00 ET · Not financial advice.</i>"
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
        "<b>⚠️ System</b>\n"
        "Daily high-tier alert cap (8) reached. Suppressing further HIGH alerts today.\n"
        "\n"
        "<i>2026-07-23 15:00 ET · Not financial advice.</i>"
    )


def test_all_interpolated_text_is_html_escaped():
    cluster = _cluster(symbol="<script>", primary_headline="a <b>bold</b> lie & a <tag>")
    text = templates.render_high_alert(cluster, _anchors(), _quote(), None, None)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert "&amp;" in text
