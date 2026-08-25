"""Missed Mover Investigation Report — read-only diagnosis of "why did
Perch miss this?"

Walks the pipeline funnel for one (symbol, session) and reports the FIRST
stage that explains the absence of an alert. Reads four stores that never
join in production — universe.db (what Perch may look at), evaluations.db
(what the detectors saw), journal.db (what Perch decided), users.db (what
was delivered) — and joins them in application code rather than by
ATTACH, so any one of them being absent degrades that stage instead of
failing the report.

DIAGNOSIS, NOT REPAIR. Nothing here proposes a threshold, and nothing
here writes: every connection is opened with SQLite's read-only URI, so
a write is refused by the database rather than merely avoided by
convention.

THE HONESTY RULES, which are the whole point:

  * Every conclusion carries an evidence class. DIRECT ROW means a row
    was read and is quoted. INFERRED means it was derived by
    conservation, and is emitted only when the tick that licenses that
    subtraction says its invariant held. NOT INSTRUMENTED means no data
    exists — said out loud rather than guessed around.

  * Absence is not evidence. No evaluation row does NOT mean
    NO_DETECTION; it means nothing was recorded, and the reason a
    promoted symbol was never evaluated is currently uninstrumented. A
    report that resolved that ambiguity would be inventing the very
    answer the reader needs.

  * Perch cannot tell you a symbol moved. data/cache is WATCHLIST-scoped,
    Stage 1's bulk daily bars are never persisted, and marks are keyed on
    detection_id — so a missed mover has none. --move-pct is supplied by
    the operator and is always labelled as such.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tradebot.config import WATCHLIST

# A 5-minute bar stamped 14:30 is not knowable until 14:35 (CLAUDE.md;
# detectors.bar_close_ts). The stores disagree about which end they
# record, and the difference is exactly one bar:
#
#   bar_evaluations.bar_ts_utc  -> the bar's OPEN
#   detections.ts_utc           -> bar_close_ts(bar), i.e. OPEN + 5m
#
# So every comparison below is made on DECISION TIME -- the moment Perch
# could first have acted on that row. Comparing a bar's open against an
# event time would credit Perch with information it did not yet have,
# which is the lookahead this project's whole bar discipline exists to
# prevent. A test pins this constant against detectors.bar_close_ts.
BAR_MINUTES = 5

# --- evidence classes ------------------------------------------------------
DIRECT_ROW = "DIRECT ROW"
INFERRED = "INFERRED"
NOT_INSTRUMENTED = "NOT INSTRUMENTED"

# --- verdicts, in funnel order --------------------------------------------
NOT_IN_UNIVERSE = "NOT_IN_UNIVERSE"
STAGE1_NOT_RUN = "STAGE1_NOT_RUN"
NOT_SCREENED = "NOT_SCREENED"
SCREENED_QUIET = "SCREENED_QUIET"
CANDIDATE_NOT_PROMOTED = "CANDIDATE_NOT_PROMOTED"
PROMOTED_NO_EVALUATION = "PROMOTED_NO_EVALUATION"
NO_EVALUATION = "NO_EVALUATION"
NO_DETECTION = "NO_DETECTION"
DETECTOR_ERROR = "DETECTOR_ERROR"
EVALUATION_ERROR = "EVALUATION_ERROR"
SUB_THRESHOLD = "SUB_THRESHOLD"
SUPPRESSED = "SUPPRESSED"
# A MEDIUM cluster that was never individually alerted is NOT suppressed --
# that is the designed route. AlertBudget.evaluate appends every
# tier=="medium" cluster to the hourly digest queue and returns
# QUEUED_FOR_DIGEST, and runner.process_new_bar writes suppress_reason
# only for SUPPRESS_CAP/COOLDOWN/NEWS_BLACKOUT/DUPLICATE -- so a normal
# medium row is alerted=0 with suppress_reason NULL, exactly like the
# genuinely-unexplained case it must not be confused with.
ROUTED_TO_DIGEST = "ROUTED_TO_DIGEST"
# A HIGH with alerted=0 and no suppression evidence anywhere. Perch's own
# records do not explain it, and it must stay visible as an anomaly rather
# than be absorbed into a normal-looking bucket.
HIGH_NOT_ALERTED_UNEXPLAINED = "HIGH_NOT_ALERTED_UNEXPLAINED"
# Success-side verdicts carry their scope IN THE NAME. The verdict token
# is what gets grepped, aggregated and quoted out of context, and a bare
# "ALERTED" will be read as "Perch caught it" by anyone who did not also
# read the window line. These say only what the data supports: something
# was surfaced inside the window the operator chose. Whether that was
# soon enough to count as catching the move is a product judgement this
# tool does not have and must not imply.
DETECTED_IN_WINDOW = "DETECTED_IN_WINDOW"
ALERT_NOT_DELIVERED_IN_WINDOW = "ALERT_NOT_DELIVERED_IN_WINDOW"
ALERT_PARTIALLY_DELIVERED_IN_WINDOW = "ALERT_PARTIALLY_DELIVERED_IN_WINDOW"
ALERTED_IN_WINDOW = "ALERTED_IN_WINDOW"
INCONCLUSIVE = "INCONCLUSIVE"
# No --event-time was supplied. The tool prints the session timeline and
# refuses to name a failure point: a symbol can be quiet at 10:00, a
# near-miss at 10:30, promoted at 11:00 and alerted at 14:31, and a
# session-wide verdict of ALERTED for a 10:35 move would be technically
# true and completely wrong. Refusing is the only honest answer.
INCONCLUSIVE_NO_EVENT_TIME = "INCONCLUSIVE_NO_EVENT_TIME"

# Printed on every report. A reader who does not know what the tool
# cannot see will over-trust what it does show.
LIMITATIONS = (
    "run_live pre-evaluation gates (no bars, not-yet-closed, stale, no new bar, "
    "no anchors) are NOT instrumented — a promoted symbol with no evaluation row "
    "has no recorded reason.",
    "Replay evaluations are not currently populated: run_replay does not write to "
    "evaluations.db, so only live runs appear here.",
    "Evaluation retention is documented but NOT enforced — absence of old rows may "
    "mean pruning, not absence of activity.",
    "market_bars availability/alignment is NOT recorded, so a NO_DETECTION cannot "
    "distinguish relative_strength_break abstaining from it genuinely comparing.",
)


@dataclass(frozen=True)
class EventWindow:
    """When the move mattered, and how long Perch had to react.

    FORWARD, not symmetric: the question is "why did Perch fail to
    surface THIS move", and Perch can only act on bars that closed at or
    after the move became visible. A backward window would ask what Perch
    knew before the move existed.

    Comparisons use decision time (see BAR_MINUTES), so nothing inside
    the window depends on information that was not knowable yet."""

    event_time: datetime
    minutes: int = 60

    @property
    def end(self) -> datetime:
        return self.event_time + timedelta(minutes=self.minutes)

    def contains(self, when: datetime | None) -> bool:
        """HALF-OPEN: [event_time, event_time + minutes).

        Start inclusive, end EXCLUSIVE. Inclusive-both-ends put an event
        landing exactly on the boundary inside two adjacent windows at
        once, so aggregating investigations ("how many misses were
        CANDIDATE_NOT_PROMOTED last month?") would count it twice. On a
        5-minute grid boundary events are common, not rare.

        The start stays inclusive because that is the load-bearing end:
        the bar decidable AT event-time is the first one Perch could have
        acted on. Consequence to read literally: --window-minutes 60
        means the twelve bars decidable in the hour BEGINNING at
        event-time, not thirteen."""
        return when is not None and self.event_time <= when < self.end

    def label(self, when: datetime | None) -> str:
        """Downstream events are labelled by their offset, so a reader can
        never mistake something that happened later for something that was
        available at event-time."""
        if when is None:
            return ""
        delta = (when - self.event_time).total_seconds() / 60.0
        if abs(delta) < 0.5:
            return " (at event-time)"
        return f" ({delta:+.0f}m {'after' if delta > 0 else 'before'} event-time)"


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _decision_time(bar_ts_utc: str):
    """When a bar's evaluation could first have been acted on."""
    ts = _parse_ts(bar_ts_utc)
    return ts + timedelta(minutes=BAR_MINUTES) if ts else None


@dataclass
class Finding:
    """One stage's result. `evidence` is attached where the conclusion is
    derived, never guessed by the formatter."""

    stage: str
    summary: str
    evidence: str
    verdict: str | None = None
    lines: list[str] = field(default_factory=list)
    explains_miss: bool = False


@dataclass
class MissReport:
    symbol: str
    session: str
    move_pct: float | None
    findings: list[Finding]
    verdict: str
    conclusion: str
    conclusion_evidence: str
    stores: dict
    window: "EventWindow | None" = None


# --------------------------------------------------------------------------
# Store access
# --------------------------------------------------------------------------


def open_readonly(path) -> sqlite3.Connection | None:
    """Read-only, and never creating.

    mode=ro refuses to create a missing file (it raises rather than
    materialising an empty database), which is exactly the behavior a
    diagnostic needs: running it must not leave new files behind, and a
    store that does not exist yet — evaluations.db before the first live
    session, users.db on a scanner-only box — is a normal state to report,
    not an error to crash on."""
    if path is None:
        return None
    try:
        return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None


def _q(conn, sql, params=()):
    """Query tolerantly: a store can predate a table this tool reads (an
    old universe.db has no screening_ticks). Missing table -> no rows,
    which the caller renders as NOT INSTRUMENTED rather than as a crash."""
    if conn is None:
        return None
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return None


# --------------------------------------------------------------------------
# Stage 1 — universe, screening, promotion
# --------------------------------------------------------------------------


def _universe_finding(universe, symbol) -> Finding:
    if universe is None:
        return Finding("universe", "universe.db not available", NOT_INSTRUMENTED)
    rows = _q(universe, "SELECT is_active, options_enabled, delisted_at FROM assets WHERE symbol = ?", (symbol,))
    if rows is None:
        return Finding("universe", "assets table not present", NOT_INSTRUMENTED)
    if not rows:
        return Finding(
            "universe", f"{symbol} is not in the universe", DIRECT_ROW,
            verdict=NOT_IN_UNIVERSE, explains_miss=True,
            lines=["Perch never had this symbol in scope, so no later stage could have seen it."],
        )
    is_active, options_enabled, delisted_at = rows[0]
    if not is_active:
        return Finding(
            "universe", f"{symbol} is in the universe but INACTIVE", DIRECT_ROW,
            verdict=NOT_IN_UNIVERSE, explains_miss=True,
            lines=[f"delisted_at={delisted_at}"],
        )
    return Finding(
        "universe", f"{symbol} active in universe", DIRECT_ROW,
        lines=[f"options_enabled={bool(options_enabled)}"],
    )


def _screening_finding(universe, symbol, session, window: EventWindow) -> Finding:
    """The Stage 1 state that GOVERNED this symbol when the move mattered.

    Not the session's strongest outcome -- that was the original bug. A
    symbol quiet at 10:00, a near-miss at 10:30 and promoted at 11:00 has
    three different Stage 1 states, and only one of them was in force at
    10:35. The governing tick is the latest one at or before event-time:
    ticks are discrete (every 30 minutes) and each one's decision stands
    until the next replaces it.

    Strictly backward-looking, so no lookahead: a tick that ran after the
    move could not have influenced whether Perch was watching during it.

    WATCHLIST symbols never reach this function -- they bypass Stage 1
    entirely, and manufacturing NOT_SCREENED for them would be wrong."""
    if universe is None:
        return Finding("stage1", "universe.db not available", NOT_INSTRUMENTED)

    ticks = _q(
        universe,
        "SELECT tick_id, tick_utc, invariant_ok, promotion_limit, screen_version "
        "FROM screening_ticks WHERE session = ? ORDER BY tick_utc",
        (session,),
    )
    if ticks is None:
        return Finding("stage1", "screening tables not present in universe.db", NOT_INSTRUMENTED)
    if not ticks:
        return Finding(
            "stage1", f"no Stage 1 screening tick recorded for {session}", DIRECT_ROW,
            verdict=STAGE1_NOT_RUN, explains_miss=True,
            lines=["Broad scan is opt-in (--broad-scan) and runs every 30 minutes; no tick "
                   "covered this session, so the screen never considered this symbol."],
        )

    governing = None
    for tick in ticks:
        when = _parse_ts(tick[1])
        if when is not None and when <= window.event_time:
            governing = tick
    if governing is None:
        earliest = ticks[0][1]
        return Finding(
            "stage1", "no screening tick ran at or before event-time — governing state unknown",
            INFERRED, verdict=INCONCLUSIVE, explains_miss=True,
            lines=[f"{len(ticks)} tick(s) this session; earliest {earliest}"
                   f"{window.label(_parse_ts(earliest))}",
                   "Every recorded tick is AFTER the move, so none of them can establish what "
                   "Stage 1 state was in force when it happened. Reporting a later tick's "
                   "outcome would be describing a different moment."],
        )

    tick_id, tick_utc, invariant_ok, promotion_limit, screen_version = governing
    base = [f"{len(ticks)} tick(s) this session; governing tick {tick_utc}"
            f"{window.label(_parse_ts(tick_utc))}",
            f"screen_version={screen_version} (Stage 1 units — never comparable to a detector score)"]

    rows = _q(
        universe,
        "SELECT outcome, screen_score, rank, reasons_json FROM screening_events "
        "WHERE tick_id = ? AND symbol = ?",
        (tick_id, symbol),
    ) or []

    if rows:
        outcome, score, rank, reasons_json = rows[0]
        reasons = json.loads(reasons_json) if reasons_json else []
        lines = base + [f"outcome={outcome}"]
        if score is not None:
            lines.append(f"screen_score={score:.3f}  rank={rank}  promotion_limit={promotion_limit}")
        if reasons:
            lines.append(f"reasons={', '.join(reasons)}")
        if outcome == "PROMOTED":
            return Finding("stage1", f"{symbol} was PROMOTED at the governing tick", DIRECT_ROW,
                           lines=lines)
        if outcome == "CANDIDATE_NOT_PROMOTED":
            if rank and promotion_limit:
                lines.append(f"missed the cut by {rank - promotion_limit} place(s)")
            return Finding(
                "stage1", f"{symbol} cleared the screen but lost to the promotion cap", DIRECT_ROW,
                verdict=CANDIDATE_NOT_PROMOTED, explains_miss=True, lines=lines)
        return Finding("stage1", f"{symbol} was screened out: {outcome}", DIRECT_ROW,
                       verdict=NOT_SCREENED, explains_miss=True, lines=lines)

    # No row at the governing tick -> quiet, but only if the counts that
    # license that subtraction actually add up FOR THAT TICK.
    if not invariant_ok:
        return Finding(
            "stage1", "cannot determine Stage 1 state: the governing tick's invariant did not hold",
            INFERRED, verdict=INCONCLUSIVE, explains_miss=True,
            lines=base + ["screening_ticks.invariant_ok = 0 for the governing tick, so "
                          "'in the universe, in no bucket, therefore quiet' cannot be trusted."])
    return Finding(
        "stage1", f"{symbol} was screened and stayed quiet at the governing tick", INFERRED,
        verdict=SCREENED_QUIET, explains_miss=True,
        lines=base + ["no screening_events row for this symbol at that tick; invariant_ok = 1",
                      "Per-symbol QUIET rows are written only in verbose audit mode; with it off "
                      "this is a subtraction, not a row."])


# --------------------------------------------------------------------------
# Stage 2 — evaluation, per run
# --------------------------------------------------------------------------


def _evaluation_findings(evaluations, symbol, session, run_id_filter, window: EventWindow) -> list[Finding]:
    """One finding per run_id, each scoped to the event window.

    Runs are never merged: a mid-session restart evaluates the same bars
    again, and collapsing them would present two independent evaluations
    as one."""
    if evaluations is None:
        return [Finding("stage2", "evaluations.db not available (no live session has written it yet?)",
                        NOT_INSTRUMENTED)]

    sessions = _q(
        evaluations,
        "SELECT eval_session_id, run_id, run_mode, evaluation_version FROM evaluation_sessions "
        "WHERE symbol = ? AND session = ? ORDER BY eval_session_id",
        (symbol, session),
    )
    if sessions is None:
        return [Finding("stage2", "evaluation tables not present in evaluations.db", NOT_INSTRUMENTED)]
    if run_id_filter:
        sessions = [x for x in sessions if x[1] == run_id_filter]
    if not sessions:
        return [Finding(
            "stage2", f"no evaluation rows for {symbol} on {session}", NOT_INSTRUMENTED,
            verdict=NO_EVALUATION,
            lines=["Absence of a row is NOT evidence that the detectors ran and found nothing.",
                   "It means nothing was recorded — the reason is not instrumented."])]

    findings = []
    for eval_session_id, run_id, run_mode, evaluation_version in sessions:
        rows = _q(
            evaluations,
            "SELECT bar_ts_utc, outcome, close, volume, atr14, kinds, cluster_score, tier, "
            "detection_id, error FROM bar_evaluations WHERE eval_session_id = ? ORDER BY bar_ts_utc",
            (eval_session_id,),
        ) or []
        findings.append(_one_run_finding(run_id, run_mode, evaluation_version, rows, window))
    return findings


def _one_run_finding(run_id, run_mode, evaluation_version, rows, window: EventWindow) -> Finding:
    """Only bars whose DECISION TIME falls inside the window may decide
    this run's verdict.

    The original bug lived here: any DETECTED bar anywhere in the session
    made the whole run non-explaining, so a 14:30 detection erased an
    11:05 miss. A detection outside the window is now reported as context
    and explicitly labelled with its offset -- it cannot exonerate a miss
    that happened earlier."""
    stage = f"stage2[run={run_id[:8]}]"
    in_window = [r for r in rows if window.contains(_decision_time(r[0]))]
    outside = [r for r in rows if not window.contains(_decision_time(r[0]))]
    header = [f"run_mode={run_mode}  run_id={run_id}  evaluation_version={evaluation_version}",
              f"{len(rows)} bar(s) evaluated this session, {len(in_window)} inside the event window"]
    for r in outside:
        if r[1] in ("DETECTED", "DETECTOR_ERROR", "EVALUATION_ERROR"):
            header.append(f"  context (outside window): {r[1]} at {r[0]}"
                          f"{window.label(_decision_time(r[0]))} — cannot bear on this move")

    if not in_window:
        return Finding(
            stage, "no bar was evaluated inside the event window", NOT_INSTRUMENTED,
            verdict=NO_EVALUATION, explains_miss=True,
            lines=header + ["Bars exist for this run but none decidable inside the window. "
                            "Why the window itself produced no evaluation is not instrumented."])

    counts = {}
    for r in in_window:
        counts[r[1]] = counts.get(r[1], 0) + 1
    header.append("in-window outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # Errors outrank everything for this run: an evaluation that crashed
    # did not complete, so "no detector fired" is not a claim about it.
    for outcome in (DETECTOR_ERROR, EVALUATION_ERROR):
        hits = [r for r in in_window if r[1] == outcome]
        if hits:
            return Finding(
                stage, f"evaluation FAILED on {len(hits)} in-window bar(s): {outcome}", DIRECT_ROW,
                verdict=outcome, explains_miss=True,
                lines=header + [f"first at {hits[0][0]}{window.label(_decision_time(hits[0][0]))}: {hits[0][9]}",
                                "An error outranks any scoring or suppression conclusion for this run."])

    detected = [r for r in in_window if r[1] == "DETECTED"]
    if detected:
        return Finding(
            stage, f"{len(detected)} in-window bar(s) produced a detection", DIRECT_ROW,
            verdict=DETECTED_IN_WINDOW,
            lines=header + [f"first at {detected[0][0]}{window.label(_decision_time(detected[0][0]))} "
                            f"kinds={detected[0][5]} score={detected[0][6]} tier={detected[0][7]}"])

    no_detection = [r for r in in_window if r[1] == "NO_DETECTION"]
    if no_detection:
        sample = max(no_detection, key=lambda r: (r[4] or 0))
        return Finding(
            stage, "every detector ran on every in-window bar and none fired", DIRECT_ROW,
            verdict=NO_DETECTION, explains_miss=True,
            lines=header + [
                f"largest-ATR in-window bar {sample[0]}{window.label(_decision_time(sample[0]))}: "
                f"close={sample[2]} volume={sample[3]} atr14={sample[4]}",
                "The stored bars and the session's frozen anchors reproduce every detector "
                "decision offline — detectors are pure, so this is a threshold question."])

    return Finding(
        stage, "every in-window bar was rejected by a data-health guard", DIRECT_ROW,
        verdict=NO_EVALUATION, explains_miss=True,
        lines=header + ["no in-window bar reached the detectors: all rows are HALTED_BAR/BAR_GAP"])


def _promotion_gap_finding(stage1: Finding, stage2_findings: list[Finding]) -> Finding | None:
    """The ambiguous case, reported as ambiguous.

    Promoted by Stage 1, no evaluation row in Stage 2. The reason lives in
    run_live's pre-evaluation gates, which record nothing — so the tool
    must NOT resolve it. Reporting PROMOTED_BUT_BLOCKED as a fact would
    invent the one answer the reader came for."""
    promoted = stage1.verdict is None and "PROMOTED" in stage1.summary
    # NOT_INSTRUMENTED means nothing was recorded AT ALL. A run that
    # evaluated bars outside the window is a different thing entirely --
    # the symbol was evaluated, just not when the move mattered -- and
    # calling that "never evaluated" would be its own false statement.
    nothing_recorded = all(
        f.evidence == NOT_INSTRUMENTED and "inside the event window" not in f.summary
        for f in stage2_findings)
    if not (promoted and nothing_recorded):
        return None
    return Finding(
        "pre-evaluation", "promoted, but never evaluated — reason NOT RECORDED", NOT_INSTRUMENTED,
        verdict=PROMOTED_NO_EVALUATION, explains_miss=True,
        lines=[
            "Stage 1 promoted this symbol and Stage 2 has no evaluation row for it.",
            "The gap between them is run_live's pre-evaluation gates: no bars, not-yet-closed bars, "
            "stale data, no new bar, no anchors. NONE of these are instrumented.",
            "This tool will NOT guess which one applied. The honest answer is that the reason "
            "is unrecorded, not that the symbol was blocked for any particular cause.",
        ],
    )


# --------------------------------------------------------------------------
# Decision & delivery
# --------------------------------------------------------------------------


def _in_window_detections(journal, symbol, session, window: EventWindow):
    """detections.ts_utc is already bar_close_ts -- the moment the cluster
    became knowable -- so it is compared to the window directly, unlike
    bar_evaluations' open timestamps."""
    rows = _q(
        journal,
        "SELECT id, ts_utc, score, tier, alerted, suppress_reason, suppress_category "
        "FROM detections WHERE symbol = ? AND session = ? ORDER BY ts_utc",
        (symbol, session),
    )
    if rows is None:
        return None, None
    inside = [r for r in rows if window.contains(_parse_ts(r[1]))]
    return inside, rows


def _decision_findings(journal, symbol, session, window: EventWindow) -> list[Finding]:
    if journal is None:
        return [Finding("decision", "journal.db not available", NOT_INSTRUMENTED)]
    inside, all_rows = _in_window_detections(journal, symbol, session, window)
    if inside is None:
        return [Finding("decision", "detections table not present", NOT_INSTRUMENTED)]

    outside_note = []
    for det_id, ts_utc, score, tier, alerted, _sr, _sc in (all_rows or []):
        if not window.contains(_parse_ts(ts_utc)):
            outside_note.append(
                f"context (outside window): detection {det_id} at {ts_utc}"
                f"{window.label(_parse_ts(ts_utc))} tier={tier} alerted={bool(alerted)}"
                " — cannot exonerate this move")

    if not inside:
        return [Finding(
            "decision", "no detection was journaled inside the event window", DIRECT_ROW,
            lines=(outside_note or ["consistent with an earlier stage explaining the miss"]))]

    findings = []
    for det_id, ts_utc, score, tier, alerted, suppress_reason, suppress_category in inside:
        lines = [f"detection {det_id} at {ts_utc}{window.label(_parse_ts(ts_utc))}: "
                 f"score={score} tier={tier} alerted={bool(alerted)}"] + outside_note
        for st, dec, reason in (_q(
            journal,
            "SELECT stage, decision, reason FROM decision_events WHERE detection_id = ? ORDER BY seq",
            (det_id,)) or []):
            lines.append(f"  ledger: {st} -> {dec}" + (f" ({reason})" if reason else ""))

        if tier == "log":
            findings.append(Finding(
                "decision", f"scored {score} — sub-threshold, journaled but never alertable",
                DIRECT_ROW, verdict=SUB_THRESHOLD, explains_miss=True, lines=lines))
        elif suppress_reason:
            # Real suppression, proven by the row itself. Checked BEFORE the
            # tier rules so a medium that genuinely was suppressed still
            # reports as suppressed.
            findings.append(Finding(
                "decision", f"suppressed: {suppress_reason}", DIRECT_ROW,
                verdict=SUPPRESSED, explains_miss=True,
                lines=lines + [f"category={suppress_category}"]))
        elif tier == "medium":
            # The designed route, not a suppression. Evidence is DIRECT ROW
            # when the ledger recorded queued_for_hourly_digest, and
            # INFERRED from tier semantics otherwise -- a journal written
            # before decision_events existed has no ledger row to quote.
            queued = any("queued_for_hourly_digest" in line for line in lines)
            findings.append(Finding(
                "decision",
                "tier=medium — routed to the hourly digest, never individually alerted",
                DIRECT_ROW if queued else INFERRED,
                verdict=ROUTED_TO_DIGEST, explains_miss=True,
                lines=lines + [
                    "This is the designed path for MEDIUM, not a suppression: "
                    "AlertBudget.evaluate queues every medium cluster for the hourly "
                    "digest and returns QUEUED_FOR_DIGEST, and no suppress_reason is "
                    "written for it.",
                    "Whether the digest itself reached anyone is NOT provable here: "
                    "send_medium_digest_if_due sends the digest with no alert_id, so "
                    "no outbox row links back to this detection.",
                ]))
        elif not alerted:
            # tier=high with alerted=0. Whether anything explains it depends
            # on the ledger: an event-window downgrade leaves detections.tier
            # at the true score-based 'high' (that column is ground truth and
            # is deliberately never rewritten by routing), so a downgraded
            # HIGH arrives here with its explanation sitting in
            # decision_events. Asserting "nothing records why" while those
            # rows print directly above would contradict the report's own
            # evidence.
            has_ledger = any(line.lstrip().startswith("ledger:") for line in lines)
            if has_ledger:
                tail = [
                    "No suppress_reason and no suppress_category — but the decision ledger "
                    "rows above DO record how this was routed (an event-window downgrade "
                    "leaves detections.tier at 'high' while routing it as medium). Read "
                    "those rows for the reason.",
                ]
            else:
                tail = [
                    "No suppress_reason, no suppress_category, and no ledger row explains "
                    "this. A HIGH that neither alerted nor recorded a reason is anomalous, "
                    "not a normal route — it is reported as unexplained rather than "
                    "labelled a suppression the data does not evidence.",
                ]
            findings.append(Finding(
                "decision",
                f"tier={tier} but never marked alerted"
                + ("" if has_ledger else ", and nothing records why"),
                DIRECT_ROW if has_ledger else NOT_INSTRUMENTED,
                verdict=HIGH_NOT_ALERTED_UNEXPLAINED, explains_miss=True,
                lines=lines + tail))
        else:
            findings.append(Finding("decision", f"alerted (tier={tier})", DIRECT_ROW, lines=lines))
    return findings


def _delivery_finding(users, journal, symbol, session, window: EventWindow) -> Finding:
    """Scoped to the detections selected for THIS window, never every
    outbox row for the symbol/session.

    Recipient granularity is preserved: one detection fans out to the ops
    channel plus each subscriber, so one delivered and one failed is
    neither a delivery nor a failure. Latency from event-time is reported
    as data -- a delivery four hours late must look different from a
    prompt one, and the product defines no 'timely' threshold for this
    tool to invent."""
    if users is None:
        return Finding("delivery", "users.db not available", NOT_INSTRUMENTED)
    if journal is None:
        return Finding("delivery", "journal.db not available — cannot resolve detection ids",
                       NOT_INSTRUMENTED)
    inside, _all_rows = _in_window_detections(journal, symbol, session, window)
    if inside is None:
        return Finding("delivery", "detections table not present", NOT_INSTRUMENTED)
    if not inside:
        return Finding("delivery", "no in-window detection to deliver", DIRECT_ROW)

    lines, totals, latencies = [], {}, []
    for det_id, *_rest in inside:
        rows = _q(users,
                  "SELECT status, delivered_at FROM outbox WHERE alert_id = ? ORDER BY id", (det_id,))
        if rows is None:
            return Finding("delivery", "outbox table not present in users.db", NOT_INSTRUMENTED)
        if not rows:
            lines.append(f"{det_id}: no outbox rows (no recipients enqueued)")
            continue
        per = {}
        for status, delivered_at in rows:
            per[status] = per.get(status, 0) + 1
            totals[status] = totals.get(status, 0) + 1
            when = _parse_ts(delivered_at)
            if when is not None:
                latencies.append((when - window.event_time).total_seconds() / 60.0)
        lines.append(f"{det_id}: " + ", ".join(f"{s}={n}" for s, n in sorted(per.items())))

    if latencies:
        lines.append(
            f"delivery_latency_from_event_time: min={min(latencies):+.0f}m "
            f"max={max(latencies):+.0f}m (reported as data — this tool defines no "
            f"'timely' threshold)")

    if not totals:
        return Finding("delivery", "no outbox rows for any in-window detection", DIRECT_ROW,
                       lines=lines)

    delivered = totals.get("delivered", 0)
    other = sum(n for st, n in totals.items() if st != "delivered")
    summary = ", ".join(f"{st}={n}" for st, n in sorted(totals.items()))
    if delivered and not other:
        return Finding("delivery", f"delivered to every recipient ({summary})", DIRECT_ROW,
                       verdict=ALERTED_IN_WINDOW, lines=lines)
    if delivered and other:
        return Finding(
            "delivery", f"PARTIAL delivery ({summary})", DIRECT_ROW,
            verdict=ALERT_PARTIALLY_DELIVERED_IN_WINDOW, explains_miss=True,
            lines=lines + ["Some recipients received it and some did not — reported as counts "
                           "rather than collapsed into a single state."])
    return Finding("delivery", f"enqueued but never delivered ({summary})", DIRECT_ROW,
                   verdict=ALERT_NOT_DELIVERED_IN_WINDOW, explains_miss=True, lines=lines)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def session_timeline(universe, evaluations, journal, users, symbol, session) -> list[str]:
    """Everything recorded about this symbol on this session, in
    chronological order — the only honest output when no event-time was
    supplied. Timestamps are shown as stored, with the store named, so a
    reader can see for themselves that a 14:31 delivery has nothing to do
    with a 10:35 move."""
    events: list[tuple[str, str]] = []

    for tick_utc, outcome, score, rank in (_q(
        universe,
        "SELECT t.tick_utc, e.outcome, e.screen_score, e.rank FROM screening_events e "
        "JOIN screening_ticks t ON t.tick_id = e.tick_id WHERE e.symbol = ? AND t.session = ?",
        (symbol, session)) or []):
        detail = f" score={score:.3f} rank={rank}" if score is not None else ""
        events.append((tick_utc, f"stage1   {outcome}{detail}"))

    for bar_ts, outcome, run_id, run_mode in (_q(
        evaluations,
        "SELECT b.bar_ts_utc, b.outcome, s.run_id, s.run_mode FROM bar_evaluations b "
        "JOIN evaluation_sessions s ON s.eval_session_id = b.eval_session_id "
        "WHERE s.symbol = ? AND s.session = ?",
        (symbol, session)) or []):
        decidable = _decision_time(bar_ts)
        events.append((decidable.isoformat() if decidable else bar_ts,
                       f"stage2   {outcome} (bar opened {bar_ts}, decidable at close) "
                       f"run={run_id[:8]}/{run_mode}"))

    detection_ids = []
    for det_id, ts_utc, score, tier, alerted in (_q(
        journal,
        "SELECT id, ts_utc, score, tier, alerted FROM detections WHERE symbol = ? AND session = ?",
        (symbol, session)) or []):
        detection_ids.append(det_id)
        events.append((ts_utc, f"decision detection {det_id} score={score} tier={tier} "
                               f"alerted={bool(alerted)}"))

    for det_id in detection_ids:
        for status, delivered_at, created_at in (_q(
            users, "SELECT status, delivered_at, created_at FROM outbox WHERE alert_id = ?",
            (det_id,)) or []):
            events.append((delivered_at or created_at, f"delivery {status} for {det_id}"))

    return [f"  {when}  {what}" for when, what in sorted(events, key=lambda e: e[0] or "")]


def _timing_finding(journal, users, symbol, session, window: EventWindow) -> Finding | None:
    """Every timestamp and latency for the in-window detections, in one
    place.

    Detection latency matters more than delivery latency and was the one
    missing from the report: it is where Perch's own responsiveness
    lives, while delivery latency mostly reflects the outbox worker.
    Reporting only the second made the first look unmeasured.

    Recipients are listed INDIVIDUALLY, each with its own status and
    time. A min/max roll-up alone would let "one delivered promptly, one
    never delivered" read as a single prompt delivery, which is the same
    collapsing the partial-delivery verdict exists to prevent.

    Every number here is DATA. This tool encodes no timeliness
    threshold, so nothing in this block is labelled timely or late."""
    if journal is None or window is None:
        return None
    inside, _all_rows = _in_window_detections(journal, symbol, session, window)
    if not inside:
        return None

    lines = [
        f"event_time                          = {window.event_time.isoformat()}",
        f"selected_window                     = [{window.event_time.isoformat()}, "
        f"{window.end.isoformat()})  (+{window.minutes}m, start inclusive, end EXCLUSIVE)",
    ]
    for det_id, ts_utc, *_rest in inside:
        detected_at = _parse_ts(ts_utc)
        lines.append(f"detection {det_id}")
        lines.append(f"  detection_time                    = {ts_utc}")
        if detected_at is not None:
            latency = (detected_at - window.event_time).total_seconds() / 60.0
            lines.append(f"  detection_latency_from_event_time = {latency:+.0f}m")
        else:
            lines.append("  detection_latency_from_event_time = unparseable timestamp")

        rows = _q(users, "SELECT chat_id, status, delivered_at FROM outbox WHERE alert_id = ? "
                         "ORDER BY id", (det_id,)) if users is not None else None
        if rows is None:
            lines.append("  delivery_time                     = NOT INSTRUMENTED (users.db unavailable)")
            continue
        if not rows:
            lines.append("  delivery_time                     = no recipients enqueued")
            continue
        for chat_id, status, delivered_at in rows:
            when = _parse_ts(delivered_at)
            if when is None:
                lines.append(f"  recipient {chat_id}: status={status}  delivery_time=none "
                             f"(never delivered)  delivery_latency_from_event_time=n/a")
            else:
                delivery_latency = (when - window.event_time).total_seconds() / 60.0
                lines.append(f"  recipient {chat_id}: status={status}  delivery_time={delivered_at}"
                             f"  delivery_latency_from_event_time={delivery_latency:+.0f}m")

    lines.append("All latencies above are reported as DATA. This tool encodes no timeliness "
                 "threshold and does not judge any of them timely or late.")
    return Finding("timing", "in-window timing and latency", DIRECT_ROW, lines=lines)


def build_report(*, symbol, session, move_pct=None, universe=None, evaluations=None,
                 journal=None, users=None, run_id=None, window: EventWindow | None = None) -> MissReport:
    """window=None means no --event-time was supplied.

    In that case the tool prints the timeline and REFUSES to name a
    failure point. A session can contain a quiet tick, a near-miss, a
    promotion, a miss, a detection and a delivery, and any session-wide
    verdict would be answering a question the operator did not ask. That
    refusal is the same discipline applied to a broken invariant and to
    the promoted/no-evaluation ambiguity."""
    findings: list[Finding] = []

    if window is None:
        timeline = session_timeline(universe, evaluations, journal, users, symbol, session)
        findings.append(Finding(
            "timeline", "no --event-time supplied — cannot scope a verdict to the move",
            NOT_INSTRUMENTED, verdict=INCONCLUSIVE_NO_EVENT_TIME,
            lines=(timeline or ["nothing recorded for this symbol on this session"])))
        return MissReport(
            symbol=symbol, session=session, move_pct=move_pct, findings=findings,
            verdict=INCONCLUSIVE_NO_EVENT_TIME, conclusion_evidence=NOT_INSTRUMENTED,
            conclusion=("No verdict: --event-time was not supplied, so no stage can be scoped to "
                        "the move. A session-wide answer would be able to report ALERTED for a "
                        "move that was actually missed hours earlier. Re-run with --event-time."),
            stores=_stores(universe, evaluations, journal, users), window=None)

    universe_finding = _universe_finding(universe, symbol)
    findings.append(universe_finding)
    if universe_finding.explains_miss:
        return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users, window)

    if symbol in WATCHLIST:
        stage1 = Finding(
            "stage1", f"{symbol} is on the fixed WATCHLIST — Stage 1 screening does not apply",
            DIRECT_ROW,
            lines=["run_live scans WATCHLIST + whatever Stage 1 promoted, so a watchlist symbol "
                   "reaches Stage 2 regardless of the screen and has no screening rows by design."])
        findings.append(stage1)
    else:
        stage1 = _screening_finding(universe, symbol, session, window)
        findings.append(stage1)
        if stage1.explains_miss:
            return _finish(symbol, session, move_pct, findings, universe, evaluations, journal,
                           users, window)

    stage2 = _evaluation_findings(evaluations, symbol, session, run_id, window)
    gap = _promotion_gap_finding(stage1, stage2)
    if gap is not None:
        findings.append(gap)
        findings.extend(stage2)
        return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users, window)
    findings.extend(stage2)

    findings.extend(_decision_findings(journal, symbol, session, window))
    findings.append(_delivery_finding(users, journal, symbol, session, window))
    timing = _timing_finding(journal, users, symbol, session, window)
    if timing is not None:
        findings.append(timing)
    return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users, window)


def _stores(universe, evaluations, journal, users) -> dict:
    return {"universe.db": universe is not None, "evaluations.db": evaluations is not None,
            "journal.db": journal is not None, "users.db": users is not None}


def _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users,
            window) -> MissReport:
    explaining = [f for f in findings if f.explains_miss]
    if explaining:
        first = explaining[0]
        verdict = first.verdict or INCONCLUSIVE
        conclusion = f"The first explainable failure point was {first.stage.upper()}: {first.summary}."
        if first.stage.startswith("stage2["):
            conclusion += " (scoped to that run — other runs are reported separately above.)"
        evidence = first.evidence
    elif any(f.verdict == ALERTED_IN_WINDOW for f in findings):
        verdict, evidence = ALERTED_IN_WINDOW, DIRECT_ROW
        conclusion = (
            "No pipeline failure identified within the selected window.\n"
            "Perch surfaced this symbol within the selected investigation window. Whether that "
            "latency was timely enough to count as catching the move is not encoded by this tool.")
    else:
        verdict, evidence = INCONCLUSIVE, NOT_INSTRUMENTED
        conclusion = ("No stage explains the miss from recorded data inside the event window. "
                      "This is an INCONCLUSIVE result, not a clean bill of health.")
    return MissReport(
        symbol=symbol, session=session, move_pct=move_pct, findings=findings, verdict=verdict,
        conclusion=conclusion, conclusion_evidence=evidence,
        stores=_stores(universe, evaluations, journal, users), window=window)


def render(report: MissReport) -> str:
    out = ["=" * 78, f"MISS REPORT — {report.symbol}, {report.session}", "=" * 78]
    move = (f"{report.move_pct:+.2f}% (supplied by operator — Perch does not retain price history "
            f"for most symbols)") if report.move_pct is not None else "not supplied"
    out.append(f"Move: {move}")
    if report.window is not None:
        out.append(f"Event window: {report.window.event_time.isoformat()} "
                   f"+{report.window.minutes}m (forward — Perch can only act on bars that closed "
                   f"at or after the move became visible)")
    else:
        out.append("Event window: NONE — no --event-time supplied")
    out += ["", "Stores: " + ", ".join(
        f"{name} {'OK' if ok else 'ABSENT'}" for name, ok in report.stores.items()), ""]

    for f in report.findings:
        marker = "**" if f.explains_miss else "  "
        out.append(f"{marker} [{f.stage}] {f.summary}")
        out.append(f"     evidence: {f.evidence}" + (f"   verdict: {f.verdict}" if f.verdict else ""))
        out += [f"     {line}" for line in f.lines]
        out.append("")

    out += ["-" * 78, f"VERDICT: {report.verdict}", f"EVIDENCE: {report.conclusion_evidence}",
            "", report.conclusion, "", "-" * 78, "KNOWN LIMITATIONS (always shown):"]
    out += [f"  - {lim}" for lim in LIMITATIONS]
    out.append("=" * 78)
    return "\n".join(out)
