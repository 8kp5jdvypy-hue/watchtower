"""Regression tests for write_cluster()'s upsert-by-identity contract.

cluster_id() hashes (symbol, session, ts_utc, kinds), so replaying a
session that already ran live reproduces the SAME detection ids and
upserts onto the live rows. scripts/replay.py does exactly that by
default -- it writes to the production journal and re-runs every cached
session -- while passing none of alerted / suppress_reason / primary_kind
/ data_feed / origin. Before this change those five were overwritten with
their parameter defaults, which for `alerted` meant a replay silently
resetting the record that an alert really fired.

The contract these pin: the upsert refreshes what the caller actually
recomputed, and preserves everything else.

Scope: write_cluster() only. run_replay() also reaches the same rows
through set_no_trade / set_news_driven / runner.py's direct UPDATEs, which
nothing here changes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradebot.detectors import Detection
from tradebot.journal import connect, write_cluster

SYMBOL = "TEST"
TS_UTC = "2026-06-15T14:00:00+00:00"
SESSION = "2026-06-15"

# Every column the upsert may NOT touch, and a non-default value for each.
PRESERVED_STATE = {
    "suppress_category": "data_integrity",
    "lifecycle_state": "confirmed",
    "related_detection_id": "some-earlier-id",
    "no_trade": 1,
    "news_driven": 1,
    "event_kind": "earnings",
    "event_severity": "downgrade",
    "extreme_mover": 1,
    "extreme_mover_gap_pct": 4.25,
    "extreme_mover_volume": 900_000,
}


def _detection(kind="level_break", score=4.0) -> Detection:
    return Detection(SYMBOL, kind, datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc), score, "headline", {"foo": "bar"})


def _base(**overrides) -> dict:
    """The arguments every caller supplies -- the detector-derived facts."""
    kwargs = dict(
        session=SESSION, symbol=SYMBOL, ts_utc=TS_UTC, kinds="level_break",
        headlines="broke prior_high", score=4.0, close=101.0, atr14=1.5, trend="up",
        detections=[_detection()], code_version_str="live-sha",
    )
    kwargs.update(overrides)
    return kwargs


def _write_live_row(conn, **overrides) -> str:
    """A row as the live pipeline leaves it: written by write_cluster, then
    enriched by the setters and runner.py's own UPDATEs."""
    detection_id = write_cluster(
        conn, **_base(**overrides),
        alerted=True, suppress_reason="cooldown_active",
        primary_kind="level_break", data_feed="sip", origin="screening",
    )
    conn.execute(
        "UPDATE detections SET "
        + ", ".join(f"{column} = ?" for column in PRESERVED_STATE)
        + " WHERE id = ?",
        (*PRESERVED_STATE.values(), detection_id),
    )
    conn.commit()
    return detection_id


def _replay_rewrite(conn, **overrides) -> str:
    """A scripts/replay.py-shaped call: detector fields only, none of the
    five omission-prone ones."""
    detection_id = write_cluster(conn, **_base(**overrides))
    conn.commit()
    return detection_id


def _row(conn, columns: str):
    return conn.execute(f"SELECT {columns} FROM detections").fetchone()


# ---------------------------------------------------------------------------
# Delivery / routing state survives a replay
# ---------------------------------------------------------------------------


def test_alerted_survives_a_replay_shaped_rewrite(tmp_path):
    """The headline regression. alerted=1 gates the whole public track
    record (/performance, the weekly recap, api/app.py); a replay resetting
    it to 0 deletes the evidence an alert really fired, and scripts/replay.py
    has no alerting stage to ever set it back."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)
    assert _row(conn, "alerted") == (1,)

    _replay_rewrite(conn)

    assert _row(conn, "alerted") == (1,)


def test_suppress_reason_survives_a_replay_shaped_rewrite(tmp_path):
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)

    _replay_rewrite(conn)

    assert _row(conn, "suppress_reason") == ("cooldown_active",)


def test_alerted_and_suppress_reason_are_still_settable_on_a_fresh_insert(tmp_path):
    """Preserved on conflict, not frozen outright: on an INSERT there is no
    prior state to protect, and 10+ existing test fixtures set alerted=True
    that way."""
    conn = connect(tmp_path / "journal.db")

    write_cluster(conn, **_base(), alerted=True, suppress_reason="daily_cap_reached")
    conn.commit()

    assert _row(conn, "alerted, suppress_reason") == (1, "daily_cap_reached")


def test_a_fresh_insert_still_defaults_to_not_alerted(tmp_path):
    conn = connect(tmp_path / "journal.db")

    _replay_rewrite(conn)

    assert _row(conn, "alerted, suppress_reason") == (0, None)


# ---------------------------------------------------------------------------
# Detector-derived facts must STILL refresh -- the point of the upsert
# ---------------------------------------------------------------------------


def test_detector_derived_fields_still_refresh_on_rewrite(tmp_path):
    """Not over-frozen. scripts/compare_replay.py's header documents A/B
    replay relying on this refresh, so preserving decision state must not
    cost it."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)

    _replay_rewrite(
        conn, headlines="broke swing_high", score=9.5, close=104.0, atr14=2.0,
        trend="down", kinds="level_break", code_version_str="replay-sha",
        detections=[_detection(score=9.5)],
        pct_from_prior_close=3.1, pct_from_prior_close_status="AVAILABLE",
    )

    row = _row(conn, "headlines, score, close, atr14, trend, tier, code_version, "
                     "pct_from_prior_close, pct_from_prior_close_status")
    assert row == ("broke swing_high", 9.5, 104.0, 2.0, "down", "high", "replay-sha", 3.1, "AVAILABLE")


def test_rewriting_still_upserts_rather_than_duplicating(tmp_path):
    conn = connect(tmp_path / "journal.db")
    first = _write_live_row(conn)

    second = _replay_rewrite(conn, score=6.0)

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Omission is not recomputation: primary_kind / data_feed
# ---------------------------------------------------------------------------


def test_omitted_primary_kind_and_data_feed_preserve_existing_values(tmp_path):
    """scripts/replay.py computes neither, so before this change it nulled
    both -- erasing the key historical_performance() samples on and the
    IEX/SIP provenance the SIP migration compares against."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)

    _replay_rewrite(conn)

    assert _row(conn, "primary_kind, data_feed") == ("level_break", "sip")


def test_explicitly_supplied_primary_kind_and_data_feed_still_refresh(tmp_path):
    """A caller that DID recompute them must still be able to correct the
    row -- preserve-if-omitted, not freeze."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)

    write_cluster(conn, **_base(), primary_kind="vwap_break", data_feed="iex")
    conn.commit()

    assert _row(conn, "primary_kind, data_feed") == ("vwap_break", "iex")


# ---------------------------------------------------------------------------
# origin: all four contract cases
# ---------------------------------------------------------------------------


def test_screening_origin_survives_an_omitted_origin_rewrite(tmp_path):
    """A broad-scan-promoted symbol must not be relabelled a watchlist
    member by a replay that never knew the difference -- that relabelling
    is exactly what docs/broad-scan-honesty-proposal.md rests on."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)
    assert _row(conn, "origin") == ("screening",)

    _replay_rewrite(conn)

    assert _row(conn, "origin") == ("screening",)


def test_fresh_insert_with_origin_omitted_still_stores_watchlist(tmp_path):
    """Backwards compatibility: the old default is preserved on the INSERT
    path, where there is no prior value that omission could mean."""
    conn = connect(tmp_path / "journal.db")

    _replay_rewrite(conn)

    assert _row(conn, "origin") == ("watchlist",)


def test_explicit_watchlist_overwrites_screening(tmp_path):
    """An explicitly supplied 'watchlist' is an assertion, not an omission,
    and must still win -- which is why the raw argument is bound separately
    for the conflict path instead of reading excluded.origin."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)

    write_cluster(conn, **_base(), origin="watchlist")
    conn.commit()

    assert _row(conn, "origin") == ("watchlist",)


def test_explicit_screening_overwrites_watchlist(tmp_path):
    conn = connect(tmp_path / "journal.db")
    write_cluster(conn, **_base(), origin="watchlist")
    conn.commit()

    write_cluster(conn, **_base(), origin="screening")
    conn.commit()

    assert _row(conn, "origin") == ("screening",)


@pytest.mark.parametrize("origin", ["watchlist", "screening"])
def test_an_explicit_origin_is_honoured_on_a_fresh_insert(tmp_path, origin):
    conn = connect(tmp_path / "journal.db")

    write_cluster(conn, **_base(), origin=origin)
    conn.commit()

    assert _row(conn, "origin") == (origin,)


# ---------------------------------------------------------------------------
# Characterization: columns the upsert has never touched, and must not start
# ---------------------------------------------------------------------------


def test_every_setter_owned_column_is_untouched_by_a_rewrite(tmp_path):
    """These are written by set_no_trade / set_news_driven / set_extreme_mover
    and runner.py's own UPDATEs. They were already safe -- absent from the
    INSERT column list entirely -- and this pins it, so a future edit that
    adds one to the SET clause fails here instead of in production."""
    conn = connect(tmp_path / "journal.db")
    _write_live_row(conn)
    columns = ", ".join(PRESERVED_STATE)
    before = _row(conn, columns)
    assert before == tuple(PRESERVED_STATE.values())  # the fixture really set them

    _replay_rewrite(conn, score=8.0, headlines="different")

    assert _row(conn, columns) == before


def test_the_upsert_set_clause_never_mentions_decision_or_delivery_state(tmp_path):
    """Reads the shipped SQL itself: the guarantee above is a property of
    the statement, and this states which columns are deliberately absent so
    the omission can't later read as an oversight."""
    import re

    from tradebot import journal

    source = re.search(
        r"ON CONFLICT\(id\) DO UPDATE SET(.*?)\n\s+\"\"\"",
        Path(journal.__file__).read_text(),
        re.S,
    ).group(1)
    assigned = set(re.findall(r"(\w+)\s*=", source.replace("COALESCE", " ")))

    for column in ("alerted", "suppress_reason", *PRESERVED_STATE):
        assert column not in assigned, f"{column} must not be assigned by the upsert"


# ---------------------------------------------------------------------------
# End to end: a live row through a full scripts/replay.py-shaped pass
# ---------------------------------------------------------------------------


def test_a_live_row_keeps_all_decision_state_through_a_replay_pass(tmp_path):
    conn = connect(tmp_path / "journal.db")
    detection_id = _write_live_row(conn)

    # Three replays, as re-running the script over a cache would do.
    for score in (5.0, 6.0, 7.0):
        _replay_rewrite(conn, score=score, headlines=f"pass {score}")

    row = conn.execute(
        "SELECT alerted, suppress_reason, primary_kind, data_feed, origin, score, headlines "
        "FROM detections WHERE id = ?",
        (detection_id,),
    ).fetchone()
    assert row == (1, "cooldown_active", "level_break", "sip", "screening", 7.0, "pass 7.0")
    assert _row(conn, ", ".join(PRESERVED_STATE)) == tuple(PRESERVED_STATE.values())
