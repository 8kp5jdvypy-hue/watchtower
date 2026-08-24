"""The replay/production boundary.

Two ways a replay could reach production, closed at the boundary rather
than by teaching individual writes to recognise a replay:

  1. The journal it writes. A replay reproduces live detection ids
     exactly (cluster_id hashes symbol/session/ts/kinds), so a replay
     aimed at data/journal.db upserts onto the live rows and then keeps
     mutating them through everything that runs after write_cluster.
     Both replay entry points now resolve their DB through one pure
     helper, journal.resolve_replay_db_path.

  2. The alerter it holds. run_replay() opens by sending a morning
     briefing and pre-open card, so --replay-date --live would have
     pushed hours-stale alerts to real subscribers before evaluating a
     single bar.

Note what is NOT claimed: replay can still write the production journal
if a human passes --allow-production-replay-db. That override exists on
purpose. The claim is that replay no longer targets production by
default or by accident, and that historical replay cannot select the
live Telegram alerter at all.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradebot import journal as journal_mod
from tradebot.alerts import ConsoleAlerter
from tradebot.detectors import Detection
from tradebot.journal import (
    ProductionJournalRefused,
    connect,
    resolve_replay_db_path,
    write_cluster,
)
import tradebot.runner as runner_mod

SESSION = date(2026, 7, 23)
SYMBOL = "TSLA"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Redirect both well-known journals into tmp_path, so no test here
    can reach the developer's real data/ tree even if the guard breaks."""
    production = tmp_path / "data" / "journal.db"
    replay = tmp_path / "data" / "journal_replay.db"
    production.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(journal_mod, "DEFAULT_DB_PATH", production)
    monkeypatch.setattr(journal_mod, "REPLAY_DB_PATH", replay)
    return {"production": production, "replay": replay, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# The pure helper
# ---------------------------------------------------------------------------


def test_no_path_resolves_to_the_replay_journal_never_production(paths):
    resolved = resolve_replay_db_path(None)

    assert resolved == paths["replay"].resolve()
    assert resolved != paths["production"].resolve()


def test_an_arbitrary_explicit_path_is_allowed_unchanged(paths):
    target = paths["tmp"] / "journal_a.db"

    assert resolve_replay_db_path(target) == target.resolve()


def test_the_production_path_stated_exactly_is_refused(paths):
    with pytest.raises(ProductionJournalRefused) as excinfo:
        resolve_replay_db_path(paths["production"])

    assert "--allow-production-replay-db" in str(excinfo.value)


@pytest.mark.parametrize("alias", [
    "data/journal.db",              # relative to the same root
    "data/../data/journal.db",      # the traversal alias called out in review
    "./data/./journal.db",
])
def test_aliases_of_the_production_path_are_refused_too(paths, monkeypatch, alias):
    """The guard compares resolved paths. A string comparison would let
    every one of these through while looking like it worked."""
    monkeypatch.chdir(paths["tmp"])

    with pytest.raises(ProductionJournalRefused):
        resolve_replay_db_path(alias)


def test_a_symlink_to_production_is_refused(paths):
    link = paths["tmp"] / "alias.db"
    paths["production"].write_bytes(b"")
    link.symlink_to(paths["production"])

    with pytest.raises(ProductionJournalRefused):
        resolve_replay_db_path(link)


def test_the_override_permits_production_exactly_and_only_when_asked(paths):
    assert resolve_replay_db_path(
        paths["production"], allow_production_db=True
    ) == paths["production"].resolve()


def test_the_refusal_is_a_valueerror(paths):
    """A ValueError subclass, so a caller that only knows it passed a bad
    path still catches it."""
    with pytest.raises(ValueError):
        resolve_replay_db_path(paths["production"])


def test_the_helper_is_pure(paths):
    """No printing, no exiting, no file creation -- the CLIs own how a
    refusal is presented."""
    resolved = resolve_replay_db_path(None)

    assert not resolved.exists()
    with pytest.raises(ProductionJournalRefused):  # not SystemExit
        resolve_replay_db_path(paths["production"])


# ---------------------------------------------------------------------------
# Programmatic run_replay -- the guard cannot live in argparse alone
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)


def _replay_cache(tmp_path: Path) -> Path:
    cache_dir = tmp_path / "cache"
    rth_open = datetime(SESSION.year, SESSION.month, SESSION.day, 13, 30, tzinfo=timezone.utc)
    _write_csv(
        cache_dir / SYMBOL / f"intraday_{SESSION.isoformat()}.csv",
        [
            {"ts": (rth_open + timedelta(minutes=5 * i)).isoformat(), "open": 100.0, "high": 100.5,
             "low": 99.5, "close": 100.0 + i * 0.1, "volume": 1000}
            for i in range(4)
        ],
    )
    _write_csv(
        cache_dir / SYMBOL / "daily.csv",
        [
            {"ts": (rth_open - timedelta(days=d)).isoformat(), "open": 99.0, "high": 101.0,
             "low": 98.0, "close": 100.0, "volume": 1_000_000}
            for d in range(5, 0, -1)
        ],
    )
    return cache_dir


@pytest.fixture
def replay_env(paths, monkeypatch):
    monkeypatch.setattr(runner_mod, "WATCHLIST", [SYMBOL])
    monkeypatch.setattr(runner_mod, "MARKET_PROXY_SYMBOLS", [])
    return _replay_cache(paths["tmp"])


def test_programmatic_run_replay_with_no_db_path_uses_the_replay_journal(
    paths, replay_env, forced_detection,
):
    """A guard that lived only in the CLI would protect the command line
    and leave every other caller -- tests, scripts, a REPL -- pointed at
    production."""
    runner_mod.run_replay(SESSION, ConsoleAlerter(), cache_dir=replay_env)

    assert paths["replay"].exists()
    assert not paths["production"].exists()
    assert sqlite3.connect(paths["replay"]).execute(
        "SELECT COUNT(*) FROM detections"
    ).fetchone()[0] > 0  # it really wrote, and wrote there


def test_programmatic_run_replay_cannot_bypass_the_guard(paths, replay_env):
    with pytest.raises(ProductionJournalRefused):
        runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=paths["production"], cache_dir=replay_env)

    assert not paths["production"].exists()


def test_programmatic_run_replay_honours_an_explicit_path(paths, replay_env):
    target = paths["tmp"] / "journal_a.db"

    runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=target, cache_dir=replay_env)

    assert target.exists()
    assert not paths["production"].exists()


def test_programmatic_run_replay_honours_the_override(paths, replay_env):
    runner_mod.run_replay(
        SESSION, ConsoleAlerter(), db_path=paths["production"],
        cache_dir=replay_env, allow_production_db=True,
    )

    assert paths["production"].exists()  # the escape hatch really works


def test_two_explicit_paths_stay_separate_the_compare_replay_shape(paths, replay_env):
    """scripts/compare_replay.py's documented workflow, and the SIP Phase 1
    IEX-vs-SIP pair: two runs, two journals, neither of them production."""
    a, b = paths["tmp"] / "journal_a.db", paths["tmp"] / "journal_b.db"

    runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=a, cache_dir=replay_env)
    runner_mod.run_replay(SESSION, ConsoleAlerter(), db_path=b, cache_dir=replay_env)

    assert a.exists() and b.exists()
    assert a.resolve() != b.resolve()
    assert not paths["production"].exists()


# ---------------------------------------------------------------------------
# Characterization: a live row cannot be altered by a default replay
# ---------------------------------------------------------------------------


DETECTION_TS = datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc)


@pytest.fixture
def forced_detection(monkeypatch):
    """Make the replay actually detect something.

    Without this the synthetic cache below fires no detector, the replay
    writes nothing at all, and every "production was not modified"
    assertion passes for the wrong reason. Same monkeypatch idiom as
    test_runner.py's _high_tier_fixture. The ts/kinds are chosen to hash
    (cluster_id) to the SAME detection id as the live row the test plants
    in production, so the replay's write_cluster + set_no_trade +
    lifecycle/suppress/alerted writes all target that exact row -- which
    is the collision this PR exists to prevent."""
    primary = Detection(SYMBOL, "gap", DETECTION_TS, 10.0, "a gap", {})
    result = {
        "ts": DETECTION_TS, "close": 100.2, "atr14": 1.0, "kinds": "gap",
        "primary_kind": "gap", "primary_headline": "a gap", "headlines": "a gap",
        "primary_detection": primary, "score": 10.0, "trend": "up", "detections": [primary],
    }
    monkeypatch.setattr(runner_mod, "evaluate_bar", lambda symbol, bars, anch, market_bars=None: result)
    return result


def _live_row(conn) -> str:
    detection_id = write_cluster(
        conn, session=SESSION.isoformat(), symbol=SYMBOL,
        ts_utc=datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc).isoformat(),
        kinds="gap", headlines="a gap", score=9.0, close=100.2, atr14=1.0, trend="up",
        detections=[Detection(SYMBOL, "gap", datetime(2026, 7, 23, 13, 40, tzinfo=timezone.utc), 9.0, "a gap", {})],
        code_version_str="live-sha", alerted=True, suppress_reason="cooldown_active",
        primary_kind="gap", data_feed="sip", origin="screening",
    )
    conn.execute(
        "UPDATE detections SET lifecycle_state='confirmed', related_detection_id='earlier', "
        "no_trade=0, news_driven=1, event_kind='earnings', suppress_category='news_blackout' WHERE id=?",
        (detection_id,),
    )
    conn.commit()
    return detection_id


def test_a_default_replay_cannot_alter_a_live_shaped_production_row(
    paths, replay_env, forced_detection,
):
    """The end-to-end property. Every write that runs after write_cluster
    -- lifecycle_state, suppress_reason/category, news_driven, alerted,
    and set_no_trade (which in replay ALWAYS writes 1, since
    ReplayMarketData has no options chain) -- lands in the replay journal
    instead, leaving the live row byte for byte as it was.

    The replay_journal assertions are the anti-vacuity control: they prove
    the replay really did perform those writes, so "production unchanged"
    means the writes were redirected, not that nothing happened. The
    positive control below closes the loop from the other side."""
    production = connect(paths["production"])
    live_id = _live_row(production)
    before = production.execute("SELECT * FROM detections").fetchall()
    production.close()

    runner_mod.run_replay(SESSION, ConsoleAlerter(), cache_dir=replay_env)

    after = sqlite3.connect(paths["production"])
    assert after.execute("SELECT * FROM detections").fetchall() == before
    assert after.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    after.close()

    # ...and the replay really did write that same detection, elsewhere.
    replayed = sqlite3.connect(paths["replay"])
    assert replayed.execute(
        "SELECT COUNT(*) FROM detections WHERE id = ?", (live_id,)
    ).fetchone()[0] == 1
    assert replayed.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] > 0
    replayed.close()


def test_positive_control_the_override_really_does_corrupt_the_live_row(
    paths, replay_env, forced_detection,
):
    """Proves the test above can fail. With the escape hatch engaged the
    replay writes straight onto the live row -- no_trade flips 0 -> 1 and
    the ledger gains replay rows -- which is exactly the damage the
    default now prevents, and exactly what the override is documented to
    permit."""
    production = connect(paths["production"])
    live_id = _live_row(production)
    assert production.execute("SELECT no_trade FROM detections").fetchone() == (0,)
    production.close()

    runner_mod.run_replay(
        SESSION, ConsoleAlerter(), db_path=paths["production"],
        cache_dir=replay_env, allow_production_db=True,
    )

    corrupted = sqlite3.connect(paths["production"])
    assert corrupted.execute(
        "SELECT no_trade FROM detections WHERE id = ?", (live_id,)
    ).fetchone() == (1,)  # relabelled NO TRADE by a replay that has no chain
    assert corrupted.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] > 0
    corrupted.close()


# ---------------------------------------------------------------------------
# CLI: tradebot.runner
# ---------------------------------------------------------------------------


class _ForbiddenTelegramAlerter:
    def __init__(self, *args, **kwargs):
        raise AssertionError("TelegramAlerter must never be constructed on a replay path")


@pytest.fixture
def cli(monkeypatch):
    """Spy run_replay/run_live and make TelegramAlerter construction an
    error, so 'was it built' is observable rather than inferred."""
    calls: list[dict] = []
    monkeypatch.setattr(runner_mod, "TelegramAlerter", _ForbiddenTelegramAlerter)
    monkeypatch.setattr(runner_mod, "configure_logging", lambda: None)
    monkeypatch.setattr(
        runner_mod, "run_replay",
        lambda session_date, alerter, **kwargs: calls.append(
            {"what": "replay", "alerter": alerter, **kwargs}
        ),
    )
    monkeypatch.setattr(
        runner_mod, "run_live",
        lambda alerter, *a, **kw: calls.append({"what": "live", "alerter": alerter, **kw}),
    )
    return calls


def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["runner.py", *argv])
    runner_mod.main()


def test_replay_plus_live_is_rejected(cli, monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, "--replay-date", "2026-07-23", "--live")

    assert excinfo.value.code != 0
    assert "cannot be combined with --live" in capsys.readouterr().err
    assert cli == []  # never dispatched


def test_replay_plus_live_plus_no_personal_alerts_is_also_rejected(cli, monkeypatch, capsys):
    """--no-personal-alerts only skips the per-user DM fan-out and still
    pushes the ops channel, so it is not an escape hatch."""
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, "--replay-date", "2026-07-23", "--live", "--no-personal-alerts")

    assert "cannot be combined with --live" in capsys.readouterr().err
    assert cli == []


def test_the_rejection_happens_before_any_telegram_alerter_exists(cli, monkeypatch):
    """_ForbiddenTelegramAlerter raises AssertionError if constructed; a
    clean SystemExit proves the guard ran first. This is the ordering the
    dispatch restructure exists to make unreachable-by-construction."""
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, "--replay-date", "2026-07-23", "--live")


def test_a_normal_replay_uses_a_console_alerter(cli, monkeypatch):
    _run_cli(monkeypatch, "--replay-date", "2026-07-23")

    assert len(cli) == 1
    assert cli[0]["what"] == "replay"
    assert isinstance(cli[0]["alerter"], ConsoleAlerter)


def test_the_cli_defaults_replay_to_no_db_path_and_no_override(cli, monkeypatch):
    _run_cli(monkeypatch, "--replay-date", "2026-07-23")

    assert cli[0]["db_path"] is None  # -> resolve_replay_db_path -> REPLAY_DB_PATH
    assert cli[0]["allow_production_db"] is False


def test_the_cli_threads_the_override_flag_through(cli, monkeypatch):
    _run_cli(monkeypatch, "--replay-date", "2026-07-23", "--allow-production-replay-db")

    assert cli[0]["allow_production_db"] is True


def test_a_refused_production_path_becomes_a_clean_cli_error(monkeypatch, paths, capsys):
    """ProductionJournalRefused surfaces as parser.error, not a traceback."""
    monkeypatch.setattr(runner_mod, "TelegramAlerter", _ForbiddenTelegramAlerter)
    monkeypatch.setattr(runner_mod, "configure_logging", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, "--replay-date", "2026-07-23", "--db-path", str(paths["production"]))

    assert excinfo.value.code != 0
    assert "refusing to replay into the production journal" in capsys.readouterr().err


def test_live_mode_still_builds_the_telegram_alerter(monkeypatch, cli):
    """The guard must not leak into live: --live without --replay-date is
    exactly as before."""
    built = []

    class _Telegram:
        def __init__(self, *a, **kw):
            built.append(True)

    monkeypatch.setattr(runner_mod, "TelegramAlerter", _Telegram)
    monkeypatch.setattr(runner_mod, "make_subscriber_hook", lambda *a, **kw: None, raising=False)

    _run_cli(monkeypatch, "--live", "--no-personal-alerts")

    assert built == [True]
    assert cli[-1]["what"] == "live"


# ---------------------------------------------------------------------------
# run_live is untouched
# ---------------------------------------------------------------------------


def test_run_live_still_defaults_to_the_production_journal():
    """The boundary is replay-only. run_live must keep resolving its
    connection the old way -- bare connect(), i.e. DEFAULT_DB_PATH."""
    import inspect

    source = inspect.getsource(runner_mod.run_live)

    assert "connect(db_path) if db_path is not None else connect()" in source
    assert "resolve_replay_db_path" not in source
    assert "allow_production_db" not in source


def test_connect_itself_still_defaults_to_production():
    import inspect

    assert inspect.signature(connect).parameters["db_path"].default == journal_mod.DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# CLI: scripts/replay.py
# ---------------------------------------------------------------------------


@pytest.fixture
def replay_script():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "replay.py"
    spec = importlib.util.spec_from_file_location("replay_boundary_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_boundary_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _script_cache(tmp_path: Path) -> Path:
    """scripts/replay.py replays the full WATCHLIST, so give it a cache it
    can find sessions in for the one symbol we monkeypatch it down to."""
    return _replay_cache(tmp_path)


def test_replay_script_with_no_db_path_never_touches_production(
    paths, replay_script, monkeypatch, capsys,
):
    monkeypatch.setattr(replay_script, "WATCHLIST", [SYMBOL])
    cache_dir = _script_cache(paths["tmp"])
    monkeypatch.setattr(sys, "argv", [
        "replay.py", "--cache-dir", str(cache_dir),
        "--out", str(paths["tmp"] / "out.csv"),
    ])

    replay_script.main()

    assert not paths["production"].exists()
    assert paths["replay"].exists()
    assert str(paths["replay"].resolve()) in capsys.readouterr().out


def test_replay_script_refuses_the_production_path(paths, replay_script, monkeypatch, capsys):
    monkeypatch.setattr(replay_script, "WATCHLIST", [SYMBOL])
    cache_dir = _script_cache(paths["tmp"])
    monkeypatch.setattr(sys, "argv", [
        "replay.py", "--cache-dir", str(cache_dir), "--out", str(paths["tmp"] / "out.csv"),
        "--db-path", str(paths["production"]),
    ])

    with pytest.raises(SystemExit) as excinfo:
        replay_script.main()

    assert excinfo.value.code != 0
    assert "refusing to replay into the production journal" in capsys.readouterr().err
    assert not paths["production"].exists()


def test_replay_script_honours_the_override(paths, replay_script, monkeypatch):
    monkeypatch.setattr(replay_script, "WATCHLIST", [SYMBOL])
    cache_dir = _script_cache(paths["tmp"])
    monkeypatch.setattr(sys, "argv", [
        "replay.py", "--cache-dir", str(cache_dir), "--out", str(paths["tmp"] / "out.csv"),
        "--db-path", str(paths["production"]), "--allow-production-replay-db",
    ])

    replay_script.main()

    assert paths["production"].exists()


def test_replay_script_honours_an_explicit_non_production_path(paths, replay_script, monkeypatch):
    monkeypatch.setattr(replay_script, "WATCHLIST", [SYMBOL])
    cache_dir = _script_cache(paths["tmp"])
    target = paths["tmp"] / "journal_sip.db"
    monkeypatch.setattr(sys, "argv", [
        "replay.py", "--cache-dir", str(cache_dir), "--out", str(paths["tmp"] / "out.csv"),
        "--db-path", str(target),
    ])

    replay_script.main()

    assert target.exists()
    assert not paths["production"].exists()
