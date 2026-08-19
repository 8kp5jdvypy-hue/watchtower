"""Read-only performance analysis over tradebot.journal's detections/marks
tables. Used by /performance and by /start's pre-onboarding track record,
so both ever say exactly the same numbers — one query path, not two
copies that could drift.

Never writes to the journal. Never reports a stat built on fewer than
MIN_HISTORY_SAMPLE data points — same discipline as tradebot.journal.

The "news-driven vs clean-technical" split uses the real detections.
news_driven column — set by runner.py from tradebot.events' actual EDGAR
filing, earnings, and FOMC/CPI/NFP/EIA event windows (see that module's
docstring), not a kind-name heuristic. If the edge lives in only one
bucket, this is how that finding surfaces: a cluster overlapping a known
event went through this same detector/scoring pipeline, but its
continuation stats don't share the same mechanism as an intraday
technical signal, so they're never blended into one number.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from tradebot.journal import MIN_HISTORY_SAMPLE

# Standard normal critical values — same "z >= ~1.96" convention
# SCANNER_PLAN.md's own before/after and best-hours checks already use
# for "statistically distinguishable from chance," not a new invention.
Z_95_TWO_SIDED = 1.959963984540054
Z_80_POWER = 0.8416212335729143

# The smallest hit rate above a coin flip worth calling a real, tradeable
# edge — used only to size "how much data would we need," never to judge
# today's actual numbers against a lower bar. Provisional like everything
# else here; revisit if real evidence ever suggests a smaller edge still
# matters.
MEANINGFUL_EDGE_HIT_RATE = 0.55


@dataclass(frozen=True)
class Slice:
    label: str
    sample_size: int
    hit_rate: float
    avg_return_pct: float


@dataclass(frozen=True)
class SignificanceCheck:
    """Whether the CURRENT hit rate is even statistically distinguishable
    from a coin flip yet, plus how much data a real (not just observed)
    edge of MEANINGFUL_EDGE_HIT_RATE would need to confirm. The second
    number does NOT depend on today's sample — it's a fixed target, since
    "how much would we need" is a different question from "do we have
    enough now." A near-zero observed edge deliberately is not projected
    forward into "you'd need N more" — an effect that's actually ~0 would
    need an undefined amount of data to ever look significant, and saying
    so plainly is more honest than a fabricated countdown."""

    z_score: float
    is_significant: bool
    n_needed_for_meaningful_edge: int


def hit_rate_z_score(hit_rate: float, sample_size: int, baseline: float = 0.5) -> float:
    """One-proportion z-test against a coin-flip baseline. |z| >= ~1.96
    is the conventional 95% threshold for "distinguishable from chance"
    at all — it says nothing about whether an effect is big enough to be
    tradeable, only whether it's there."""
    if sample_size <= 0:
        return 0.0
    standard_error = math.sqrt(baseline * (1 - baseline) / sample_size)
    if standard_error == 0:
        return 0.0
    return (hit_rate - baseline) / standard_error


def required_sample_size(
    target_hit_rate: float, baseline: float = 0.5, alpha_z: float = Z_95_TWO_SIDED, power_z: float = Z_80_POWER
) -> int:
    """Standard two-sided sample-size formula for a single proportion:
    how many observations a one-proportion z-test needs to reliably
    (95% confidence, 80% power) tell target_hit_rate apart from baseline,
    if that's really the true rate."""
    delta = target_hit_rate - baseline
    if delta == 0:
        raise ValueError("target_hit_rate must differ from baseline")
    return math.ceil(((alpha_z + power_z) ** 2 * baseline * (1 - baseline)) / (delta**2))


def significance_check(hit_rate: float, sample_size: int) -> SignificanceCheck:
    z = hit_rate_z_score(hit_rate, sample_size)
    return SignificanceCheck(
        z_score=z,
        is_significant=abs(z) >= Z_95_TWO_SIDED,
        n_needed_for_meaningful_edge=required_sample_size(MEANINGFUL_EDGE_HIT_RATE),
    )


@dataclass(frozen=True)
class TrackRecord:
    tier: str
    offset_min: int
    sample_size: int
    hit_rate: float
    avg_return_pct: float
    max_drawdown_pct: float
    longest_losing_streak: int
    news_driven: Slice | None
    clean_technical: Slice | None
    total_alerts: int
    total_no_trade: int
    no_trade_tracked_count: int
    significance: SignificanceCheck


def _signed_return_pct(close: float, price: float, trend: str) -> float:
    """The one sign convention every continuation/return stat in this
    module shares — see the historical_performance() avg_return_pct sign
    bug (b813185) this project already got burned by once. `trend` is
    which direction the alert favored ('up' or 'down'); a positive
    result always means "the alert's own direction played out," never a
    literal price-went-up reading."""
    raw = (price - close) / close * 100
    return raw if trend == "up" else -raw


def _signed_returns(
    conn: sqlite3.Connection,
    tier: str,
    offset_min: int,
    since: str | None = None,
    until: str | None = None,
    alerted_only: bool = False,
) -> list[dict]:
    """alerted_only=False (default, every existing caller) measures every
    HIGH-tier detection the journal ever recorded, alerted or not — the
    right population for "is our detection edge real." alerted_only=True
    restricts to detections that were actually sent (d.alerted = 1) —
    the right, and only correct, population for anything shown to the
    public as a record of what subscribers received (see
    docs/phase4-proof-engine-proposal.md's "finding that changes the
    design"). Same query, same sign convention, two different
    populations answering two different questions — never blend them."""
    query = """
        SELECT d.ts_utc, d.close, d.trend, d.news_driven, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.tier = ?
    """
    params: list = [offset_min, tier]
    if alerted_only:
        query += " AND d.alerted = 1"
    if since is not None:
        query += " AND d.ts_utc >= ?"
        params.append(since)
    if until is not None:
        query += " AND d.ts_utc < ?"
        params.append(until)
    query += " ORDER BY d.ts_utc"
    rows = conn.execute(query, params).fetchall()
    out = []
    for ts_utc, close, trend, news_driven, price in rows:
        signed = _signed_return_pct(close, price, trend)
        out.append({"ts_utc": ts_utc, "return_pct": signed, "news_driven": bool(news_driven)})
    return out


def _slice_stats(label: str, returns: list[dict]) -> Slice | None:
    if len(returns) < MIN_HISTORY_SAMPLE:
        return None
    hits = sum(1 for r in returns if r["return_pct"] > 0)
    return Slice(
        label=label,
        sample_size=len(returns),
        hit_rate=hits / len(returns),
        avg_return_pct=sum(r["return_pct"] for r in returns) / len(returns),
    )


def _max_drawdown_pct(returns: list[dict]) -> float:
    """Peak-to-trough on a hypothetical equal-weighted, back-to-back
    cumulative curve of these returns taken in chronological order — NOT
    real compounded P&L (these are overlapping 30m windows on different
    symbols, not sequential trades of one account). Stated as
    "hypothetical" everywhere it's surfaced."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r["return_pct"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    return max_dd


def _longest_losing_streak(returns: list[dict]) -> int:
    longest = current = 0
    for r in returns:
        if r["return_pct"] <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def track_record(
    conn: sqlite3.Connection, tier: str = "high", offset_min: int = 30, alerted_only: bool = False
) -> TrackRecord | None:
    returns = _signed_returns(conn, tier, offset_min, alerted_only=alerted_only)
    if len(returns) < MIN_HISTORY_SAMPLE:
        return None

    news = [r for r in returns if r["news_driven"]]
    technical = [r for r in returns if not r["news_driven"]]

    total_alerts = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE tier = ? AND alerted = 1", (tier,)
    ).fetchone()[0]
    no_trade_row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN no_trade = 1 THEN 1 ELSE 0 END) "
        "FROM detections WHERE tier = ? AND alerted = 1 AND no_trade IS NOT NULL",
        (tier,),
    ).fetchone()
    tracked_count, no_trade_count = no_trade_row
    no_trade_count = no_trade_count or 0
    hit_rate = sum(1 for r in returns if r["return_pct"] > 0) / len(returns)

    return TrackRecord(
        tier=tier,
        offset_min=offset_min,
        sample_size=len(returns),
        hit_rate=hit_rate,
        avg_return_pct=sum(r["return_pct"] for r in returns) / len(returns),
        max_drawdown_pct=_max_drawdown_pct(returns),
        longest_losing_streak=_longest_losing_streak(returns),
        news_driven=_slice_stats("news-driven", news),
        clean_technical=_slice_stats("clean technical", technical),
        total_alerts=total_alerts,
        total_no_trade=no_trade_count,
        no_trade_tracked_count=tracked_count,
        significance=significance_check(hit_rate, len(returns)),
    )


# --------------------------------------------------------------------------
# Weekly recap — see tradebot.rendering.templates.render_weekly_recap. The
# SAME shape and rendering path runs every week regardless of outcome, so
# a bad week is never structurally softer than a good one: there is no
# separate "good week" template that could hide a bad one behind less
# detail. A week with too few tracked alerts reports that plainly rather
# than fabricating a rate — same MIN_HISTORY_SAMPLE discipline as
# everywhere else, but unlike track_record(), never returns None: a thin
# week still gets a recap, just one that says so.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyRecap:
    week_start: str
    week_end: str
    tier: str
    offset_min: int
    sample_size: int
    hit_rate: float | None
    avg_return_pct: float | None
    significance: SignificanceCheck | None
    total_alerts: int
    total_no_trade: int
    no_trade_tracked_count: int


def weekly_recap(
    conn: sqlite3.Connection,
    week_start: str,
    week_end: str,
    tier: str = "high",
    offset_min: int = 30,
    alerted_only: bool = False,
) -> WeeklyRecap:
    """[week_start, week_end) — week_end is exclusive, so callers pass
    the next week's start date, not the last included day."""
    returns = _signed_returns(conn, tier, offset_min, since=week_start, until=week_end, alerted_only=alerted_only)

    total_alerts = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE tier = ? AND alerted = 1 AND ts_utc >= ? AND ts_utc < ?",
        (tier, week_start, week_end),
    ).fetchone()[0]
    no_trade_row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN no_trade = 1 THEN 1 ELSE 0 END) FROM detections "
        "WHERE tier = ? AND alerted = 1 AND no_trade IS NOT NULL AND ts_utc >= ? AND ts_utc < ?",
        (tier, week_start, week_end),
    ).fetchone()
    tracked_count, no_trade_count = no_trade_row
    no_trade_count = no_trade_count or 0

    hit_rate = None
    avg_return_pct = None
    significance = None
    if len(returns) >= MIN_HISTORY_SAMPLE:
        hit_rate = sum(1 for r in returns if r["return_pct"] > 0) / len(returns)
        avg_return_pct = sum(r["return_pct"] for r in returns) / len(returns)
        significance = significance_check(hit_rate, len(returns))

    return WeeklyRecap(
        week_start=week_start,
        week_end=week_end,
        tier=tier,
        offset_min=offset_min,
        sample_size=len(returns),
        hit_rate=hit_rate,
        avg_return_pct=avg_return_pct,
        significance=significance,
        total_alerts=total_alerts,
        total_no_trade=no_trade_count,
        no_trade_tracked_count=tracked_count,
    )


# --------------------------------------------------------------------------
# Public record row listing (docs/phase4-proof-engine-proposal.md, Part A
# and Part B). Unlike track_record()/weekly_recap(), always alerted-only
# by construction — there is no "everything" mode here, because a row
# with nothing to verify a real send against has no business appearing
# on a page whose whole premise is "this was really sent." Reads two
# databases (journal.db's detections/marks, users.db's outbox) because
# the verifiable send time lives in the outbox, not in detections.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicAlertRow:
    detection_id: str
    symbol: str
    headline: str
    trend: str  # "up" | "down"
    origin: str  # "watchlist" | "screening" -- a visible column, never a default filter
    sent_at: str  # real outbox.delivered_at, ISO -- the verifiable timestamp
    offset_min: int
    return_pct: float | None  # None until graded
    tracked: bool  # whether a mark exists yet at offset_min


def _primary_headline(headlines: str, kinds: str, primary_kind: str | None) -> str:
    """The one sentence render_high_alert() actually put in the sent
    message's rationale line (cluster.primary_headline), not the full
    semicolon-joined `headlines` -- that's every constituent detection's
    headline, a superset nobody ever received verbatim (see alerts.Cluster's
    own docstring). Recovered by position: kinds/headlines are written in
    the same detection order (journal.write_cluster), so primary_kind's
    index in `kinds` locates it in `headlines` too -- same technique
    api.app._context_summary already uses for the same reason. Rows
    written before primary_kind existed (NULL) fall back to the full
    joined string -- the closest honest answer available, not a guess."""
    if not primary_kind:
        return headlines
    kinds_list = kinds.split(",")
    if primary_kind not in kinds_list:
        return headlines
    headline_list = headlines.split("; ")
    idx = kinds_list.index(primary_kind)
    if idx >= len(headline_list):
        return headlines
    return headline_list[idx]


def public_alert_history(
    journal_conn: sqlite3.Connection,
    users_conn: sqlite3.Connection,
    *,
    tier: str = "high",
    offset_min: int = 30,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[PublicAlertRow]:
    """Every real alert, sent-timestamp verified, newest first.

    A HIGH detection appears here the moment it's actually delivered
    (outbox.status = 'delivered'), not the moment it's detected or
    marked alerted=1 in journal.db (that flag means "send was
    initiated," not "confirmed delivered" -- see runner.process_new_bar).
    A detection with no matching delivered outbox row -- in flight, or
    never actually sent live (a replay/dev run) -- does not appear at
    all, rather than with a fabricated or missing timestamp. This is
    what makes the record append-only in practice, not just in
    intent: a row can only ever go from "not here yet" to "here, with
    a real time," never the reverse and never edited in place.

    A row still appears before it's graded (return_pct=None,
    tracked=False) -- the same "pending, never hidden" rule the
    dashboard's own Signals view already uses; excluding ungraded
    rows would just be cherry-picking on a delay.

    since/until bound d.ts_utc (detection time, indexed and always
    within seconds of the real send) for the SQL scan; the returned
    list is then sorted by the real sent_at, since that (not
    detection time) is the property "append-only sequence" refers to.

    tier/offset_min: see track_record()'s docstring for why 30m is the
    canonical horizon this project already treats as the answer to
    "did it work" everywhere else the question comes up."""
    query = (
        "SELECT id, symbol, kinds, headlines, primary_kind, trend, origin, close, ts_utc "
        "FROM detections WHERE tier = ? AND alerted = 1"
    )
    params: list = [tier]
    if since is not None:
        query += " AND ts_utc >= ?"
        params.append(since)
    if until is not None:
        query += " AND ts_utc < ?"
        params.append(until)
    rows = journal_conn.execute(query, params).fetchall()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))

    marks_by_id = dict(
        journal_conn.execute(
            f"SELECT detection_id, price FROM marks WHERE offset_min = ? AND detection_id IN ({placeholders})",
            [offset_min, *ids],
        ).fetchall()
    )
    sent_by_id = dict(
        users_conn.execute(
            f"SELECT alert_id, MIN(delivered_at) FROM outbox "
            f"WHERE status = 'delivered' AND alert_id IN ({placeholders}) GROUP BY alert_id",
            ids,
        ).fetchall()
    )

    result = []
    for id_, symbol, kinds, headlines, primary_kind, trend, origin, close, _ts_utc in rows:
        sent_at = sent_by_id.get(id_)
        if sent_at is None:
            continue
        price = marks_by_id.get(id_)
        result.append(
            PublicAlertRow(
                detection_id=id_,
                symbol=symbol,
                headline=_primary_headline(headlines, kinds, primary_kind),
                trend=trend,
                origin=origin or "watchlist",
                sent_at=sent_at,
                offset_min=offset_min,
                return_pct=_signed_return_pct(close, price, trend) if price is not None else None,
                tracked=price is not None,
            )
        )
    result.sort(key=lambda r: r.sent_at, reverse=True)
    if limit is not None:
        result = result[:limit]
    return result


# --------------------------------------------------------------------------
# /example — a real win and a real day's hit rate, randomly picked from
# the journal each time. See tradebot.rendering.templates.render_example.
#
# The randomness is which REAL record gets shown, never a generated
# number: every field here comes straight out of detections/marks, the
# same tables /performance reads. Never invent a result to make a
# demo/marketing moment look better — see the incident that prompted
# this design (a request for a "random win generator" was declined in
# favor of this).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RealWin:
    detection_id: str
    symbol: str
    kinds: str
    headline: str
    trend: str  # "up" | "down" -> which option side this favored (see render_example)
    close: float
    mark_price: float
    return_pct: float  # signed, always > 0 by construction (this IS a win)
    offset_min: int
    ts_utc: str


def random_real_win(
    conn: sqlite3.Connection, tier: str = "high", offset_min: int = 30, rng=None, notable_percentile: float = 90
) -> RealWin | None:
    """A random REAL winning detection — restricted to the top
    `notable_percentile` of real wins currently in the journal (by
    return_pct), recomputed live every call rather than a fixed cutoff,
    so it can't quietly go stale as more sessions accumulate.

    This is a deliberate design choice, not an oversight: the median
    real win in this journal is under 1%, so a uniform random pick over
    EVERY win mostly shows unremarkable ones. Restricting the pool to a
    disclosed top slice — and having render_example LABEL it as "one of
    the more notable real wins," not a typical one — is honest curation.
    Quietly cherry-picking the single best win and presenting it as if
    it were a neutral sample would not be; the difference is disclosure,
    not the underlying data. See journal.hour_performance's docstring
    for the same "don't pretend a filtered view is the whole picture"
    discipline applied elsewhere in this project.

    None if the journal has no real win to show yet, never a fabricated
    one to fill the gap."""
    import random as _random

    rng = rng or _random
    rows = conn.execute(
        """
        SELECT d.id, d.symbol, d.kinds, d.headlines, d.trend, d.close, m.price, d.ts_utc
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.tier = ?
        """,
        (offset_min, tier),
    ).fetchall()
    wins = []
    for detection_id, symbol, kinds, headlines, trend, close, price, ts_utc in rows:
        signed = _signed_return_pct(close, price, trend)
        if signed > 0:
            wins.append(
                RealWin(
                    detection_id=detection_id, symbol=symbol, kinds=kinds, headline=headlines, trend=trend,
                    close=close, mark_price=price, return_pct=signed, offset_min=offset_min, ts_utc=ts_utc,
                )
            )
    if not wins:
        return None
    wins.sort(key=lambda w: w.return_pct)
    cutoff = int(len(wins) * notable_percentile / 100)
    notable = wins[cutoff:] or wins[-1:]
    return rng.choice(notable)


@dataclass(frozen=True)
class DayHitRate:
    session: str
    hit_rate: float
    sample_size: int
    offset_min: int


def random_real_day_hit_rate(
    conn: sqlite3.Connection, tier: str = "high", offset_min: int = 30, min_sample: int = MIN_HISTORY_SAMPLE, rng=None
) -> DayHitRate | None:
    """A uniformly random REAL session's real hit rate — only among
    sessions with at least min_sample tracked alerts that day, same
    never-a-stat-on-too-little-data discipline as everywhere else.
    None if no real session clears that bar yet."""
    import random as _random

    rng = rng or _random
    rows = conn.execute(
        """
        SELECT d.session, d.close, d.trend, m.price
        FROM detections d
        JOIN marks m ON m.detection_id = d.id AND m.offset_min = ?
        WHERE d.tier = ?
        """,
        (offset_min, tier),
    ).fetchall()
    by_session: dict[str, list[bool]] = {}
    for session, close, trend, price in rows:
        signed = _signed_return_pct(close, price, trend)
        by_session.setdefault(session, []).append(signed > 0)

    candidates = [
        DayHitRate(session=session, hit_rate=sum(hits) / len(hits), sample_size=len(hits), offset_min=offset_min)
        for session, hits in by_session.items()
        if len(hits) >= min_sample
    ]
    if not candidates:
        return None
    return rng.choice(candidates)
