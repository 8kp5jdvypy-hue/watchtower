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
from pathlib import Path

from tradebot.config import WATCHLIST

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
ALERT_NOT_DELIVERED = "ALERT_NOT_DELIVERED"
ALERT_PARTIALLY_DELIVERED = "ALERT_PARTIALLY_DELIVERED"
ALERTED = "ALERTED"
INCONCLUSIVE = "INCONCLUSIVE"

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


def _screening_finding(universe, symbol, session) -> Finding:
    """Stage 1 for a symbol that goes through it.

    WATCHLIST symbols do not: run_live scans WATCHLIST + whatever Stage 1
    promoted, so a watchlist name is evaluated regardless of the screen
    and has no screening rows by construction. Reporting that as
    NOT_SCREENED would be flatly wrong, which is why the caller checks
    membership first."""
    if universe is None:
        return Finding("stage1", "universe.db not available", NOT_INSTRUMENTED)

    ticks = _q(
        universe,
        "SELECT tick_id, tick_utc, invariant_ok, promotion_limit, counts_json, screen_version, "
        "run_id, run_mode FROM screening_ticks WHERE session = ? ORDER BY tick_utc",
        (session,),
    )
    if ticks is None:
        return Finding("stage1", "screening tables not present in universe.db", NOT_INSTRUMENTED)
    if not ticks:
        return Finding(
            "stage1", f"no Stage 1 screening tick recorded for {session}", DIRECT_ROW,
            verdict=STAGE1_NOT_RUN, explains_miss=True,
            lines=["Broad scan is opt-in (--broad-scan) and runs every 30 minutes; "
                   "no tick covered this session, so the screen never considered this symbol."],
        )

    events = _q(
        universe,
        "SELECT t.tick_utc, e.outcome, e.screen_score, e.rank, e.reasons_json, t.promotion_limit, "
        "t.screen_version FROM screening_events e JOIN screening_ticks t ON t.tick_id = e.tick_id "
        "WHERE e.symbol = ? AND t.session = ? ORDER BY t.tick_utc",
        (symbol, session),
    ) or []

    tick_line = f"{len(ticks)} screening tick(s) recorded for {session}"

    if events:
        best = _best_screening_event(events)
        tick_utc, outcome, score, rank, reasons_json, promotion_limit, screen_version = best
        reasons = json.loads(reasons_json) if reasons_json else []
        lines = [
            tick_line,
            f"tick {tick_utc}: outcome={outcome}",
            f"screen_version={screen_version} (Stage 1 units — never comparable to a detector score)",
        ]
        if score is not None:
            lines.append(f"screen_score={score:.3f}  rank={rank}  promotion_limit={promotion_limit}")
        if reasons:
            lines.append(f"reasons={', '.join(reasons)}")
        if len(events) > 1:
            lines.append(f"({len(events)} screening event(s) this session; strongest shown)")

        if outcome == "PROMOTED":
            return Finding("stage1", f"{symbol} was PROMOTED to Stage 2", DIRECT_ROW, lines=lines)
        if outcome == "CANDIDATE_NOT_PROMOTED":
            short_by = (rank - promotion_limit) if (rank and promotion_limit) else None
            if short_by:
                lines.append(f"missed the cut by {short_by} place(s)")
            return Finding(
                "stage1", f"{symbol} cleared the screen but lost to the promotion cap", DIRECT_ROW,
                verdict=CANDIDATE_NOT_PROMOTED, explains_miss=True, lines=lines,
            )
        # MISSING_FROM_FETCH / INSUFFICIENT_HISTORY / INVALID_BASELINE / UNEXPECTED
        return Finding(
            "stage1", f"{symbol} was screened out: {outcome}", DIRECT_ROW,
            verdict=NOT_SCREENED, explains_miss=True, lines=lines,
        )

    # No direct row. The only remaining reading is "screened and quiet" --
    # and that is a subtraction, valid only while the invariant holds.
    invariant_ok = all(t[2] for t in ticks)
    audit_note = ("Per-symbol QUIET rows are written only in verbose audit mode; "
                  "with it off this is a subtraction, not a row.")
    if not invariant_ok:
        return Finding(
            "stage1", "cannot determine Stage 1 outcome: conservation invariant did not hold", INFERRED,
            verdict=INCONCLUSIVE, explains_miss=True,
            lines=[tick_line,
                   "screening_ticks.invariant_ok = 0 for at least one tick, so the counts do not add "
                   "up and 'in the universe, in no bucket, therefore quiet' cannot be trusted."],
        )
    return Finding(
        "stage1", f"{symbol} was screened and stayed quiet (no threshold crossed)", INFERRED,
        verdict=SCREENED_QUIET, explains_miss=True,
        lines=[tick_line, "no screening_events row for this symbol; every tick's invariant_ok = 1",
               audit_note],
    )


def _best_screening_event(events):
    """The strongest event of the session — a symbol can be quiet at 10:00
    and a near-miss at 14:30, and the near-miss is the one that explains
    the day."""
    priority = {
        "PROMOTED": 0, "CANDIDATE_NOT_PROMOTED": 1, "INVALID_BASELINE": 2,
        "INSUFFICIENT_HISTORY": 3, "MISSING_FROM_FETCH": 4,
    }
    return sorted(events, key=lambda e: (priority.get(e[1], 9), -(e[2] or 0)))[0]


# --------------------------------------------------------------------------
# Stage 2 — evaluation, per run
# --------------------------------------------------------------------------


def _evaluation_findings(evaluations, symbol, session, run_id_filter) -> list[Finding]:
    """One finding per run_id. Runs are never merged: a mid-session
    restart produces a second run over the same bars, and collapsing them
    would present two independent evaluations as one."""
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
        sessions = [s for s in sessions if s[1] == run_id_filter]
    if not sessions:
        return [Finding(
            "stage2", f"no evaluation rows for {symbol} on {session}", NOT_INSTRUMENTED,
            verdict=NO_EVALUATION,
            lines=["Absence of a row is NOT evidence that the detectors ran and found nothing.",
                   "It means nothing was recorded — the reason is not instrumented."],
        )]

    findings = []
    for eval_session_id, run_id, run_mode, evaluation_version in sessions:
        rows = _q(
            evaluations,
            "SELECT bar_ts_utc, outcome, close, volume, atr14, kinds, cluster_score, tier, "
            "detection_id, error FROM bar_evaluations WHERE eval_session_id = ? ORDER BY bar_ts_utc",
            (eval_session_id,),
        ) or []
        findings.append(_one_run_finding(run_id, run_mode, evaluation_version, rows))
    return findings


def _one_run_finding(run_id, run_mode, evaluation_version, rows) -> Finding:
    stage = f"stage2[run={run_id[:8]}]"
    header = [f"run_mode={run_mode}  run_id={run_id}  evaluation_version={evaluation_version}",
              f"{len(rows)} bar(s) evaluated"]
    if not rows:
        return Finding(stage, "session row exists but no bars were evaluated", NOT_INSTRUMENTED,
                       verdict=NO_EVALUATION, lines=header)

    counts = {}
    for r in rows:
        counts[r[1]] = counts.get(r[1], 0) + 1
    header.append("outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # Errors outrank any downstream conclusion for this run: a crashed
    # detector means the evaluation did not complete, so "nothing fired"
    # is not a finding that can be made about it.
    for outcome, verdict in ((DETECTOR_ERROR, DETECTOR_ERROR), (EVALUATION_ERROR, EVALUATION_ERROR)):
        hits = [r for r in rows if r[1] == outcome]
        if hits:
            return Finding(
                stage, f"evaluation FAILED on {len(hits)} bar(s): {outcome}", DIRECT_ROW,
                verdict=verdict, explains_miss=True,
                lines=header + [f"first at {hits[0][0]}: {hits[0][9]}",
                                "An error outranks any scoring or suppression conclusion for this run — "
                                "the evaluation did not complete, so 'no detector fired' cannot be claimed."],
            )

    detected = [r for r in rows if r[1] == "DETECTED"]
    if detected:
        lines = header + [f"detected on {len(detected)} bar(s); first at {detected[0][0]} "
                          f"kinds={detected[0][5]} score={detected[0][6]} tier={detected[0][7]}"]
        return Finding(stage, f"{len(detected)} bar(s) produced a detection", DIRECT_ROW,
                       lines=lines)

    no_detection = [r for r in rows if r[1] == "NO_DETECTION"]
    if no_detection:
        sample = max(no_detection, key=lambda r: (r[4] or 0))
        return Finding(
            stage, "every detector ran on every bar and none fired", DIRECT_ROW,
            verdict=NO_DETECTION, explains_miss=True,
            lines=header + [
                f"largest-ATR bar {sample[0]}: close={sample[2]} volume={sample[3]} atr14={sample[4]}",
                "The stored bars and the session's frozen anchors reproduce every detector decision "
                "offline — detectors are pure, so this is a threshold question, not a data question.",
            ],
        )

    # Only HALTED_BAR / BAR_GAP rows.
    return Finding(
        stage, "every evaluated bar was rejected by a data-health guard", DIRECT_ROW,
        verdict=NO_EVALUATION, explains_miss=True,
        lines=header + ["no bar reached the detectors: all rows are HALTED_BAR/BAR_GAP"],
    )


def _promotion_gap_finding(stage1: Finding, stage2_findings: list[Finding]) -> Finding | None:
    """The ambiguous case, reported as ambiguous.

    Promoted by Stage 1, no evaluation row in Stage 2. The reason lives in
    run_live's pre-evaluation gates, which record nothing — so the tool
    must NOT resolve it. Reporting PROMOTED_BUT_BLOCKED as a fact would
    invent the one answer the reader came for."""
    promoted = stage1.verdict is None and "PROMOTED" in stage1.summary
    no_eval = all(f.verdict == NO_EVALUATION or f.evidence == NOT_INSTRUMENTED for f in stage2_findings)
    if not (promoted and no_eval):
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


def _decision_findings(journal, symbol, session) -> list[Finding]:
    if journal is None:
        return [Finding("decision", "journal.db not available", NOT_INSTRUMENTED)]
    dets = _q(
        journal,
        "SELECT id, ts_utc, score, tier, alerted, suppress_reason, suppress_category "
        "FROM detections WHERE symbol = ? AND session = ? ORDER BY ts_utc",
        (symbol, session),
    )
    if dets is None:
        return [Finding("decision", "detections table not present", NOT_INSTRUMENTED)]
    if not dets:
        return [Finding("decision", "no detection was journaled", DIRECT_ROW,
                        lines=["consistent with an earlier stage explaining the miss"])]

    findings = []
    for det_id, ts_utc, score, tier, alerted, suppress_reason, suppress_category in dets:
        lines = [f"detection {det_id} at {ts_utc}: score={score} tier={tier} alerted={bool(alerted)}"]
        ledger = _q(
            journal,
            "SELECT stage, decision, reason FROM decision_events WHERE detection_id = ? ORDER BY seq",
            (det_id,),
        ) or []
        for st, dec, reason in ledger:
            lines.append(f"  ledger: {st} -> {dec}" + (f" ({reason})" if reason else ""))

        if tier == "log":
            findings.append(Finding(
                "decision", f"scored {score} — sub-threshold, journaled but never alertable", DIRECT_ROW,
                verdict=SUB_THRESHOLD, explains_miss=True, lines=lines))
        elif suppress_reason:
            findings.append(Finding(
                "decision", f"suppressed: {suppress_reason}", DIRECT_ROW,
                verdict=SUPPRESSED, explains_miss=True,
                lines=lines + [f"category={suppress_category}"]))
        elif not alerted:
            findings.append(Finding(
                "decision", f"tier={tier} but never marked alerted", DIRECT_ROW,
                verdict=SUPPRESSED, explains_miss=True,
                lines=lines + ["no suppress_reason recorded — check the ledger rows above"]))
        else:
            findings.append(Finding("decision", f"alerted (tier={tier})", DIRECT_ROW, lines=lines))
    return findings


def _delivery_finding(users, journal, symbol, session) -> Finding:
    """Per-recipient, never collapsed.

    One detection fans out to many outbox rows (the ops channel plus each
    subscriber). One delivered and one failed is NOT a delivery, and it is
    not a failure either — it is a partial, and the report says so with
    counts rather than picking a side."""
    if users is None:
        return Finding("delivery", "users.db not available", NOT_INSTRUMENTED)
    if journal is None:
        return Finding("delivery", "journal.db not available — cannot resolve detection ids",
                       NOT_INSTRUMENTED)
    dets = _q(journal, "SELECT id FROM detections WHERE symbol = ? AND session = ?", (symbol, session)) or []
    if not dets:
        return Finding("delivery", "no detection to deliver", DIRECT_ROW)

    lines, totals = [], {}
    for (det_id,) in dets:
        rows = _q(users, "SELECT status, COUNT(*) FROM outbox WHERE alert_id = ? GROUP BY status", (det_id,))
        if rows is None:
            return Finding("delivery", "outbox table not present in users.db", NOT_INSTRUMENTED)
        if not rows:
            lines.append(f"{det_id}: no outbox rows (no recipients enqueued)")
            continue
        per = ", ".join(f"{s}={n}" for s, n in sorted(rows))
        lines.append(f"{det_id}: {per}")
        for s, n in rows:
            totals[s] = totals.get(s, 0) + n

    if not totals:
        return Finding("delivery", "no outbox rows for any detection this session", DIRECT_ROW, lines=lines)

    delivered = totals.get("delivered", 0)
    other = sum(n for s, n in totals.items() if s != "delivered")
    summary = ", ".join(f"{s}={n}" for s, n in sorted(totals.items()))
    if delivered and not other:
        return Finding("delivery", f"delivered to every recipient ({summary})", DIRECT_ROW,
                       verdict=ALERTED, lines=lines)
    if delivered and other:
        return Finding(
            "delivery", f"PARTIAL delivery ({summary})", DIRECT_ROW,
            verdict=ALERT_PARTIALLY_DELIVERED, explains_miss=True,
            lines=lines + ["Some recipients received it and some did not — "
                           "reported as counts rather than collapsed into a single state."])
    return Finding("delivery", f"enqueued but never delivered ({summary})", DIRECT_ROW,
                   verdict=ALERT_NOT_DELIVERED, explains_miss=True, lines=lines)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_report(*, symbol, session, move_pct=None, universe=None, evaluations=None,
                 journal=None, users=None, run_id=None) -> MissReport:
    findings: list[Finding] = []

    universe_finding = _universe_finding(universe, symbol)
    findings.append(universe_finding)
    if universe_finding.explains_miss:
        return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users)

    in_watchlist = symbol in WATCHLIST
    if in_watchlist:
        findings.append(Finding(
            "stage1", f"{symbol} is on the fixed WATCHLIST — Stage 1 screening does not apply",
            DIRECT_ROW,
            lines=["run_live scans WATCHLIST + whatever Stage 1 promoted, so a watchlist symbol "
                   "reaches Stage 2 regardless of the screen and has no screening rows by design."]))
        stage1 = findings[-1]
    else:
        stage1 = _screening_finding(universe, symbol, session)
        findings.append(stage1)
        if stage1.explains_miss:
            return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users)

    stage2 = _evaluation_findings(evaluations, symbol, session, run_id)
    gap = _promotion_gap_finding(stage1, stage2)
    if gap is not None:
        findings.append(gap)
        findings.extend(stage2)
        return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users)
    findings.extend(stage2)

    decision = _decision_findings(journal, symbol, session)
    findings.extend(decision)
    findings.append(_delivery_finding(users, journal, symbol, session))
    return _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users)


def _finish(symbol, session, move_pct, findings, universe, evaluations, journal, users) -> MissReport:
    explaining = [f for f in findings if f.explains_miss]
    if explaining:
        first = explaining[0]
        verdict = first.verdict or INCONCLUSIVE
        run_scoped = first.stage.startswith("stage2[")
        conclusion = f"The first explainable failure point was {first.stage.upper()}: {first.summary}."
        if run_scoped:
            conclusion += " (scoped to that run — other runs are reported separately above.)"
        evidence = first.evidence
    elif any(f.verdict == ALERTED for f in findings):
        verdict, evidence = ALERTED, DIRECT_ROW
        conclusion = "No failure point: this symbol was detected, alerted and delivered."
    else:
        verdict, evidence = INCONCLUSIVE, NOT_INSTRUMENTED
        conclusion = ("No stage explains the miss from recorded data. This is an INCONCLUSIVE "
                      "result, not a clean bill of health.")
    return MissReport(
        symbol=symbol, session=session, move_pct=move_pct, findings=findings,
        verdict=verdict, conclusion=conclusion, conclusion_evidence=evidence,
        stores={"universe.db": universe is not None, "evaluations.db": evaluations is not None,
                "journal.db": journal is not None, "users.db": users is not None},
    )


def render(report: MissReport) -> str:
    out = ["=" * 78, f"MISS REPORT — {report.symbol}, {report.session}", "=" * 78]
    move = f"{report.move_pct:+.2f}% (supplied by operator — Perch does not retain price history " \
           f"for most symbols)" if report.move_pct is not None else "not supplied"
    out += [f"Move: {move}", ""]
    out.append("Stores: " + ", ".join(
        f"{name} {'OK' if ok else 'ABSENT'}" for name, ok in report.stores.items()))
    out.append("")

    for f in report.findings:
        marker = "**" if f.explains_miss else "  "
        out.append(f"{marker} [{f.stage}] {f.summary}")
        out.append(f"     evidence: {f.evidence}" + (f"   verdict: {f.verdict}" if f.verdict else ""))
        for line in f.lines:
            out.append(f"     {line}")
        out.append("")

    out += ["-" * 78, f"VERDICT: {report.verdict}", f"EVIDENCE: {report.conclusion_evidence}",
            "", report.conclusion, "", "-" * 78, "KNOWN LIMITATIONS (always shown):"]
    for lim in LIMITATIONS:
        out.append(f"  - {lim}")
    out.append("=" * 78)
    return "\n".join(out)
