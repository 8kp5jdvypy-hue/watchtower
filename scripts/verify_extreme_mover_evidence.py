#!/usr/bin/env python3
"""Ship #2 (P3) validation: runs the actual shipped
tradebot.guard.extreme_mover_evidence() against real cached bars,
looking for any >25%-from-open excursion and checking whether it would
now clear the guard instead of being silently suppressed.

Same discovery method as the proposal doc's own draft validation
(docs/open-awareness-proposals-2026-08.md, Proposal 3): walks every
cached intraday CSV under the given cache dir (watchlist AND screening
symbols both have a dir there once they've fired a detection — see
runner._cache_todays_intraday_bars), finds the first bar whose close is
more than 25% from the session's opening print, then re-plays
extreme_mover_evidence() bar-by-bar from that point forward. Unlike the
doc's draft (which only eyeballed bar-by-bar closes+volume), this calls
the real shipped predicate, so a "would now alert" verdict here is not
a guess.

anchors/quote are minimal stand-ins (not full DailyAnchors/Quote) --
extreme_mover_evidence only ever reads anchors.prior_close and
quote.last, so a SimpleNamespace with just those two attributes is a
faithful call, not an approximation of the function's real inputs. The
session's OPEN (first bar's open) stands in for prior_close, same
approximation the doc's own draft script used, needed because a
screening symbol's daily.csv (which would carry the real prior close)
usually isn't cached -- only fetch_cache.py's watchlist run fetches
daily bars; the close-time cache writer only ever writes intraday.

Usage (see docs/DEPLOYMENT.md's "Running scripts/ tools in-container"
for the docker compose run invocation):
    python3 scripts/verify_extreme_mover_evidence.py
    python3 scripts/verify_extreme_mover_evidence.py --glob 'intraday_2026-08-1[12].csv'
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.guard import ExtremeMoverEvidence, extreme_mover_evidence
from tradebot.marketdata import _is_rth, _read_bars

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
GAP_THRESHOLD_PCT = 0.25


@dataclass(frozen=True)
class MoverSession:
    symbol: str
    path: Path
    prior_close: float  # approximated as the session's own opening print -- see module docstring
    first_excursion_bar: int
    bar_log: list  # [(bar_index, Bar, gap_pct, ExtremeMoverEvidence | None), ...]
    verified: tuple[int, ExtremeMoverEvidence] | None  # (bar_index, evidence) of the first verification, if any


def find_mover_sessions(cache_dir: Path, glob_pattern: str = "intraday_*.csv") -> list[MoverSession]:
    """One MoverSession per cached file with any >25%-from-open bar,
    each carrying a full bar-by-bar replay of extreme_mover_evidence()
    from the first excursion onward. Empty list means nothing in this
    cache tree ever crossed the line -- not necessarily that the guard
    has nothing to prove, just that this cache_dir has no such sessions."""
    sessions: list[MoverSession] = []
    for path in sorted(cache_dir.glob(f"*/{glob_pattern}")):
        symbol = path.parent.name
        bars = [b for b in _read_bars(path, symbol) if _is_rth(b)]
        if not bars:
            continue
        prior_close = bars[0].open
        hits = [i for i, b in enumerate(bars) if abs(b.close - prior_close) / prior_close > GAP_THRESHOLD_PCT]
        if not hits:
            continue
        first_i = hits[0]

        anchors = SimpleNamespace(prior_close=prior_close)
        bar_log = []
        verified: tuple[int, ExtremeMoverEvidence] | None = None
        for i in range(first_i, len(bars)):
            quote = SimpleNamespace(last=bars[i].close)
            evidence = extreme_mover_evidence(bars[: i + 1], anchors, quote)
            gap_pct = abs(bars[i].close - prior_close) / prior_close
            bar_log.append((i, bars[i], gap_pct, evidence))
            if evidence is not None and verified is None:
                verified = (i, evidence)

        sessions.append(MoverSession(symbol, path, prior_close, first_i, bar_log, verified))
    return sessions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--glob", default="intraday_*.csv", help="filename glob within each symbol dir")
    args = parser.parse_args()

    sessions = find_mover_sessions(args.cache_dir, args.glob)
    if not sessions:
        print(f"no >25% excursions found under {args.cache_dir} matching {args.glob}")
        return

    for s in sessions:
        print(f"\n== {s.symbol} {s.path.name} -- session open {s.prior_close:g}, first >25% excursion at bar{s.first_excursion_bar}")
        for i, bar, gap_pct, evidence in s.bar_log:
            flag = "VERIFIED" if evidence is not None else "-"
            print(f"  bar{i:2d} {bar.ts:%H:%M}Z close={bar.close:<10g} vol={bar.volume:>10,} gap={gap_pct * 100:5.1f}%  {flag}")
        if s.verified is not None:
            i, evidence = s.verified
            print(
                f"  -> would now alert at bar{i}: EXTREME MOVER {evidence.gap_pct * 100:.1f}% vs prior close "
                f"-- verified across 2 bars, {evidence.verified_volume:,} shares"
            )
        else:
            print("  -> never verifies in this session -- the guard was right, still suppresses")


if __name__ == "__main__":
    main()
