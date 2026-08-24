"""Stage 2 evaluation observability — what the detectors saw, bar by bar.

The remaining black hole after Stage 1 (tradebot.universe's
screening_ticks/screening_events): a symbol that reaches
runner.process_new_bar and produces no detection leaves no trace
anywhere. evaluate_bar returns None, process_new_bar returns, and
nothing is written — so "the detectors looked at TSLA at 14:35 and
nothing fired" and "TSLA was never evaluated at all" are the same
observation afterwards, which is to say no observation.

WHY RECORDING THE INPUTS IS ENOUGH. Every detector is pure (CLAUDE.md:
data in, Detection|None out, no I/O, no clock, no globals — verified by
grep, not assumption). So Detection = f(bars, anchors, market_bars), and
storing the inputs stores the explanation: any detector, at any
threshold, current or future, can be re-run offline against exactly what
it saw. That is why this layer needs no detector change at all, now or
later, and why it records bars rather than trying to extract
"why didn't you fire" reasons that detectors have no API to give.

Anchors are computed once per session and frozen (CLAUDE.md), so they
belong on the session row, not repeated on all ~78 bar rows.

Deliberately a separate store from data/journal.db, and not only for
size. process_new_bar holds a journal.db transaction whose boundary is
load-bearing — runner._commit_then_send commits precisely so a detection
is durable before any alert referencing it exists. Two of the outcomes
here are recorded BEFORE that function ever opens its transaction, and a
write into journal.db at that point would leave one open that
process_new_bar then abandons. A separate file on its own connection
makes interference structurally impossible rather than merely avoided.

Deliberately NOT tradebot.journal's decision_events, for four
independent reasons, any one sufficient: that table's detection_id is
NOT NULL and three of the outcomes here have no detection; its own
schema comment already excludes "a bar with no detection" as a reviewed
scope decision; ~3,200 mostly-nothing rows a session would bury the ~5
rows per detection it exists to surface; and its append-only
RAISE(ABORT) triggers are incompatible with the retention these rows
need.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "evaluations.db"

logger = logging.getLogger("watchtower.evaluations")

# Bump when the MEANING of an evaluation record changes: a new or
# redefined outcome, a change to which inputs are captured, or a change
# to what a row is emitted for. Rows carrying different versions are not
# comparable, and a query spanning sessions should filter on it. Same
# hand-bumped discipline as broad_scan.SCREEN_VERSION, for the same
# reason: an auto-derived hash cannot be forgotten but also cannot be
# reasoned about.
EVALUATION_VERSION = 1

# Reached process_new_bar and a cluster was written. Recorded too, so the
# funnel is complete and "was this bar evaluated at all?" is a direct
# lookup rather than an inference from absence.
OUTCOME_DETECTED = "DETECTED"
# Zero-volume bar — is_halted_bar. Returns before evaluate_bar.
OUTCOME_HALTED_BAR = "HALTED_BAR"
# A hole in the series — is_bar_gap. Returns before evaluate_bar.
OUTCOME_BAR_GAP = "BAR_GAP"
# THE black hole: every detector ran and none fired.
OUTCOME_NO_DETECTION = "NO_DETECTION"
# evaluate_bar raised — a detector crashed, or the lookahead assertion
# tripped. Recorded and then RE-RAISED: the exception must keep
# propagating exactly as it did before, or this instrumentation would
# have changed behavior.
OUTCOME_DETECTOR_ERROR = "DETECTOR_ERROR"
# Something in the evaluation path failed outside evaluate_bar itself.
# Also recorded and re-raised.
OUTCOME_EVALUATION_ERROR = "EVALUATION_ERROR"

MAX_EVALUATION_JSON_LEN = 20000  # anchors carry avg_cum_volume_by_bar (~78 entries)

SCHEMA = """
-- One row per (symbol, session, run). Holds the frozen anchors, which
-- are session-scoped by construction and would otherwise be repeated on
-- every bar row.
CREATE TABLE IF NOT EXISTS evaluation_sessions (
    eval_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    symbol TEXT NOT NULL,
    -- Which execution produced these. A replay drives process_new_bar
    -- too, and reproduces the same (symbol, session, bar_ts) exactly --
    -- without attribution a replay's evaluations would be
    -- indistinguishable from the live ones, the corruption class the
    -- journal spent three changes closing. In the UNIQUE key, so a
    -- replay gets its own session row instead of colliding with live's.
    run_id TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    evaluation_version INTEGER NOT NULL,
    code_version TEXT,
    origin TEXT,
    anchors_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session, symbol, run_id)
);
CREATE INDEX IF NOT EXISTS idx_evaluation_sessions_symbol ON evaluation_sessions(symbol, session);

-- One row per evaluated bar. The bar's own OHLCV is stored rather than
-- any derived ratio: detectors are pure, so the raw series plus the
-- session's anchors reproduce every decision exactly, and a stored
-- derivation would be a second thing that can disagree.
CREATE TABLE IF NOT EXISTS bar_evaluations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    -- evaluation_sessions.eval_session_id. Loose reference, no FK --
    -- same style as marks.detection_id / decision_events.detection_id.
    eval_session_id INTEGER NOT NULL,
    -- The bar's OPEN timestamp (CLAUDE.md: a 5-minute bar stamped 14:30
    -- is not knowable until 14:35). The decision about it is taken at
    -- its close.
    bar_ts_utc TEXT NOT NULL,
    outcome TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    -- As evaluate_bar resolved it (the primary detector's own ATR when
    -- it has one, else the bars-window ATR) -- never recomputed here.
    atr14 REAL,
    kinds TEXT,
    cluster_score REAL,
    tier TEXT,
    -- journal.db detections.id when a cluster was written. The join key
    -- across the two files; NULL for every non-detection outcome, which
    -- is the whole reason these rows cannot live in decision_events.
    detection_id TEXT,
    error TEXT,
    UNIQUE(eval_session_id, bar_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bar_evaluations_session ON bar_evaluations(eval_session_id, bar_ts_utc);
CREATE INDEX IF NOT EXISTS idx_bar_evaluations_outcome ON bar_evaluations(outcome);
"""

# Retention, DOCUMENTED BUT NOT ENFORCED -- nothing here deletes.
# Shipping a deleter in the change that first creates the data means the
# first bug in it destroys the only copy.
#
#   <=42 symbols x ~78 five-minute bars = ~3,300 rows/session (~650 KB),
#   ~165 MB/year. Bar-shaped cardinality, not universe-shaped, which is
#   why -- unlike Stage 1's screening_events -- nothing here needs to be
#   aggregated to stay affordable. Suggested horizon: 90 days.
#
# A pruning job is a separate change.


@dataclass(frozen=True)
class BarEvaluation:
    """One bar's evaluation, as read back."""

    bar_ts_utc: str
    outcome: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    atr14: float | None
    kinds: str | None
    cluster_score: float | None
    tier: str | None
    detection_id: str | None
    error: str | None
    run_id: str
    run_mode: str
    evaluation_version: int


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """db_path=None resolves DEFAULT_DB_PATH at CALL time, not in the
    signature default. A signature default is bound when the function is
    defined, so a test that monkeypatches the module attribute would be
    silently ignored and the suite would write the developer's real
    evaluations.db -- the same trap tradebot.metrics documents for its
    own path resolution."""
    db_path = Path(db_path if db_path is not None else DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def _encode(value) -> str | None:
    """All-or-nothing, same as journal.record_decision_event: an
    oversized document is dropped rather than truncated, because half a
    JSON document is not a smaller fact, it is an unparseable one."""
    if not value:
        return None
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded) > MAX_EVALUATION_JSON_LEN:
        logger.warning("evaluation JSON is %d bytes (limit %d) -- recording without it",
                       len(encoded), MAX_EVALUATION_JSON_LEN)
        return None
    return encoded


def ensure_session(
    conn: sqlite3.Connection,
    *,
    session: str,
    symbol: str,
    run_id: str,
    run_mode: str,
    now_utc: str,
    code_version: str | None = None,
    origin: str | None = None,
    anchors: dict | None = None,
) -> int:
    """Get-or-create the (session, symbol, run_id) row, returning its id.

    Idempotent by the UNIQUE key rather than by a read-then-write race:
    every bar of a session calls this, and the first one wins. anchors
    are written only on creation — they are frozen for the session, so a
    later bar has nothing new to say about them, and re-writing them
    would quietly turn a frozen fact into a mutable one."""
    conn.execute(
        """
        INSERT INTO evaluation_sessions
            (session, symbol, run_id, run_mode, evaluation_version, code_version,
             origin, anchors_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session, symbol, run_id) DO NOTHING
        """,
        (session, symbol, run_id, run_mode, EVALUATION_VERSION, code_version,
         origin, _encode(anchors), now_utc),
    )
    return conn.execute(
        "SELECT eval_session_id FROM evaluation_sessions WHERE session=? AND symbol=? AND run_id=?",
        (session, symbol, run_id),
    ).fetchone()[0]


def record_bar_evaluation(
    conn: sqlite3.Connection,
    *,
    session: str,
    symbol: str,
    run_id: str,
    run_mode: str,
    now_utc: str,
    bar_ts_utc: str,
    outcome: str,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    atr14: float | None = None,
    kinds: str | None = None,
    cluster_score: float | None = None,
    tier: str | None = None,
    detection_id: str | None = None,
    error: str | None = None,
    code_version: str | None = None,
    origin: str | None = None,
    anchors: dict | None = None,
) -> int:
    """Record one bar's evaluation, creating the session row if needed.

    Upserts on (eval_session_id, bar_ts_utc): re-evaluating the same bar
    in the same run supersedes rather than duplicates. That is not a
    normal path — process_new_bar is called once per NEW bar — but a
    restart mid-session can legitimately re-present one, and two rows
    claiming to be the same bar's evaluation would be worse than one
    that was rewritten.

    Commits on its own connection. This file is deliberately not
    journal.db, so that commit cannot interact with the transaction
    process_new_bar is holding open (see this module's docstring)."""
    eval_session_id = ensure_session(
        conn, session=session, symbol=symbol, run_id=run_id, run_mode=run_mode,
        now_utc=now_utc, code_version=code_version, origin=origin, anchors=anchors,
    )
    conn.execute(
        """
        INSERT INTO bar_evaluations
            (eval_session_id, bar_ts_utc, outcome, open, high, low, close, volume,
             atr14, kinds, cluster_score, tier, detection_id, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(eval_session_id, bar_ts_utc) DO UPDATE SET
            outcome=excluded.outcome, open=excluded.open, high=excluded.high,
            low=excluded.low, close=excluded.close, volume=excluded.volume,
            atr14=excluded.atr14, kinds=excluded.kinds,
            cluster_score=excluded.cluster_score, tier=excluded.tier,
            detection_id=excluded.detection_id, error=excluded.error
        """,
        (eval_session_id, bar_ts_utc, outcome, open, high, low, close, volume,
         atr14, kinds, cluster_score, tier, detection_id, error),
    )
    conn.commit()
    return eval_session_id


def evaluation_history_for_symbol(
    conn: sqlite3.Connection, symbol: str, session: str, run_id: str | None = None,
) -> list[BarEvaluation]:
    """Every bar this symbol was evaluated on, in bar order — the
    "the detectors looked and found nothing, here is exactly what they
    were looking at" query.

    run_id=None returns every run's rows; a caller comparing a replay
    against the live session should pass one, since both produce rows for
    the same bars by construction."""
    sql = """
        SELECT b.bar_ts_utc, b.outcome, b.open, b.high, b.low, b.close, b.volume,
               b.atr14, b.kinds, b.cluster_score, b.tier, b.detection_id, b.error,
               s.run_id, s.run_mode, s.evaluation_version
        FROM bar_evaluations b
        JOIN evaluation_sessions s ON s.eval_session_id = b.eval_session_id
        WHERE s.symbol = ? AND s.session = ?
    """
    params: list = [symbol, session]
    if run_id is not None:
        sql += " AND s.run_id = ?"
        params.append(run_id)
    sql += " ORDER BY b.bar_ts_utc, b.seq"
    return [BarEvaluation(*row) for row in conn.execute(sql, params).fetchall()]


def session_anchors(conn: sqlite3.Connection, symbol: str, session: str, run_id: str) -> dict | None:
    """The frozen anchors this run evaluated against — the other half of
    what a detector needs to be re-run offline against a stored bar."""
    row = conn.execute(
        "SELECT anchors_json FROM evaluation_sessions WHERE symbol=? AND session=? AND run_id=?",
        (symbol, session, run_id),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else None
