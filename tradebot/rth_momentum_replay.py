"""Offline truth replay for the final-RTH momentum handoff contract."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import exchange_calendars as ecals

from tradebot.detectors import Bar
from tradebot.rth_momentum import (
    OUTCOME_CANDIDATE,
    RTH_MOMENTUM_VERSION,
    evaluate_rth_momentum,
    rth_thresholds,
)


TRUTH_SCHEMA_VERSION = 1
DEFAULT_TRUTH_PATH = (
    Path(__file__).resolve().parent.parent / "truth" / "rth_momentum_v1.json"
)
ALLOWED_COHORTS = {"tuning", "contract_holdout", "empirical_holdout"}
ALLOWED_LABELS = {"positive", "negative", "ambiguous"}
ALLOWED_FAULTS = {None, "naive_timestamp"}
CALENDAR = ecals.get_calendar("XNYS")
THRESHOLD_FIELDS = {
    "move_pct",
    "persistence_bars",
    "minimum_cumulative_notional",
    "maximum_data_age_seconds",
    "bar_timeframe",
    "window_lead_minutes",
}


@dataclass(frozen=True)
class RthTruthCase:
    case_id: str
    cohort: str
    label: str
    symbol: str
    event_date: date
    session_open: datetime
    session_close: datetime
    evaluation_offsets_min: tuple[float, ...]
    expected_candidate: bool | None
    expected_final_outcome: str
    expected_first_candidate_offset_min: float | None
    eligible_offset_min: float | None
    provenance: str
    notes: str
    prior_close: float | None
    bars: tuple[dict[str, Any], ...]
    fault: str | None


@dataclass(frozen=True)
class RthReplayCaseResult:
    case_id: str
    cohort: str
    label: str
    symbol: str
    provenance: str
    expected_candidate: bool | None
    observed_candidate: bool
    expected_final_outcome: str
    final_outcome: str
    expected_first_candidate_offset_min: float | None
    first_candidate_offset_min: float | None
    detection_latency_seconds: float | None
    candidate_observations: int
    unique_candidates: int
    duplicate_candidate_observations: int
    passed: bool


@dataclass(frozen=True)
class RthReplayMetrics:
    definitive_cases: int
    ambiguous_cases: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    exact_outcome_accuracy: float
    cases_passed: int
    cases_failed: int
    candidate_observations: int
    unique_candidates: int
    duplicate_candidate_observations: int
    data_quality_candidate_leaks: int
    mean_detection_latency_seconds: float | None
    max_detection_latency_seconds: float | None


@dataclass(frozen=True)
class RthReplayBaseline:
    name: str
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True)
class RthReplayReport:
    truth_set_version: str
    schema_version: int
    truth_momentum_version: int
    momentum_version: int
    cohort: str
    truth_thresholds: dict[str, Any]
    thresholds: dict[str, Any]
    contract_compatible: bool
    baseline: RthReplayBaseline
    metrics: RthReplayMetrics
    cases: tuple[RthReplayCaseResult, ...]
    evidence_boundary: str


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _finite(raw: Any, context: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{context} must be numeric")
    value = float(raw)
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError(f"{context} must be finite")
    return value


def _aware_datetime(raw: Any, context: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must be an ISO-8601 string")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_case(raw: Any, index: int) -> RthTruthCase:
    context = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    case_id = _required(raw, "id", context)
    cohort = _required(raw, "cohort", context)
    label = _required(raw, "label", context)
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"{context}.id must be a non-empty string")
    if cohort not in ALLOWED_COHORTS:
        raise ValueError(f"{context}.cohort must be one of {sorted(ALLOWED_COHORTS)}")
    if label not in ALLOWED_LABELS:
        raise ValueError(f"{context}.label must be one of {sorted(ALLOWED_LABELS)}")
    symbol = _required(raw, "symbol", context)
    if (
        not isinstance(symbol, str)
        or not symbol
        or symbol != symbol.strip().upper()
    ):
        raise ValueError(f"{context}.symbol must be canonical")
    try:
        event_date = date.fromisoformat(_required(raw, "event_date", context))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}.event_date must be an ISO date") from exc
    session_open = _aware_datetime(
        _required(raw, "session_open_utc", context),
        f"{context}.session_open_utc",
    )
    session_close = _aware_datetime(
        _required(raw, "session_close_utc", context),
        f"{context}.session_close_utc",
    )
    if not CALENDAR.is_session(event_date):
        raise ValueError(f"{context}.event_date must be an XNYS session")
    expected_open = CALENDAR.session_open(event_date).to_pydatetime().astimezone(
        timezone.utc
    )
    expected_close = CALENDAR.session_close(event_date).to_pydatetime().astimezone(
        timezone.utc
    )
    if session_open != expected_open or session_close != expected_close:
        raise ValueError(f"{context} session bounds do not match XNYS")
    offsets_raw = _required(raw, "evaluation_offsets_min", context)
    if not isinstance(offsets_raw, list) or not offsets_raw:
        raise ValueError(f"{context}.evaluation_offsets_min must be non-empty")
    offsets = tuple(
        _finite(value, f"{context}.evaluation_offsets_min")
        for value in offsets_raw
    )
    if list(offsets) != sorted(offsets) or len(offsets) != len(set(offsets)):
        raise ValueError(f"{context}.evaluation_offsets_min must be ordered and unique")
    if offsets[0] < -30 or offsets[-1] > 0:
        raise ValueError(f"{context}.evaluation_offsets_min must stay in the final 30 minutes")
    expected_candidate = _required(raw, "expected_candidate", context)
    if expected_candidate is not None and not isinstance(expected_candidate, bool):
        raise ValueError(f"{context}.expected_candidate must be true, false, or null")
    if label == "positive" and expected_candidate is not True:
        raise ValueError(f"{context} positive label must expect a candidate")
    if label == "negative" and expected_candidate is not False:
        raise ValueError(f"{context} negative label must reject a candidate")
    if label == "ambiguous" and expected_candidate is not None:
        raise ValueError(f"{context} ambiguous label must not declare candidate truth")
    expected_outcome = _required(raw, "expected_final_outcome", context)
    if not isinstance(expected_outcome, str) or not expected_outcome:
        raise ValueError(f"{context}.expected_final_outcome must be non-empty")
    first_raw = raw.get("expected_first_candidate_offset_min")
    first = (
        _finite(first_raw, f"{context}.expected_first_candidate_offset_min")
        if first_raw is not None
        else None
    )
    eligible_raw = raw.get("eligible_offset_min")
    eligible = (
        _finite(eligible_raw, f"{context}.eligible_offset_min")
        if eligible_raw is not None
        else None
    )
    if expected_candidate is True and (first is None or eligible is None):
        raise ValueError(f"{context} candidate truth requires first and eligible offsets")
    if first is not None and first not in offsets:
        raise ValueError(f"{context}.expected_first_candidate_offset_min must be evaluated")
    if first is not None and eligible is not None and eligible > first:
        raise ValueError(f"{context}.eligible_offset_min cannot follow first detection")
    prior_raw = raw.get("prior_close")
    prior_close = _finite(prior_raw, f"{context}.prior_close") if prior_raw is not None else None
    if prior_close is not None and prior_close <= 0:
        raise ValueError(f"{context}.prior_close must be positive")
    bars = raw.get("bars", [])
    if not isinstance(bars, list) or any(not isinstance(bar, dict) for bar in bars):
        raise ValueError(f"{context}.bars must be a list of objects")
    fault = raw.get("fault")
    if fault not in ALLOWED_FAULTS:
        raise ValueError(
            f"{context}.fault must be one of "
            f"{sorted(str(value) for value in ALLOWED_FAULTS)}"
        )
    provenance = _required(raw, "provenance", context)
    notes = _required(raw, "notes", context)
    if not isinstance(provenance, str) or not provenance:
        raise ValueError(f"{context}.provenance must be non-empty")
    if not isinstance(notes, str) or not notes:
        raise ValueError(f"{context}.notes must be non-empty")
    return RthTruthCase(
        case_id=case_id,
        cohort=cohort,
        label=label,
        symbol=symbol,
        event_date=event_date,
        session_open=session_open,
        session_close=session_close,
        evaluation_offsets_min=offsets,
        expected_candidate=expected_candidate,
        expected_final_outcome=expected_outcome,
        expected_first_candidate_offset_min=first,
        eligible_offset_min=eligible,
        provenance=provenance,
        notes=notes,
        prior_close=prior_close,
        bars=tuple(bars),
        fault=fault,
    )


def load_rth_truth_set(
    path: Path | str = DEFAULT_TRUTH_PATH,
) -> tuple[str, int, dict[str, Any], tuple[RthTruthCase, ...]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("RTH truth set root must be an object")
    schema_version = _required(payload, "schema_version", "truth set")
    if schema_version != TRUTH_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise ValueError(f"unsupported RTH truth schema {schema_version!r}")
    version = _required(payload, "truth_set_version", "truth set")
    if not isinstance(version, str) or not version:
        raise ValueError("truth_set_version must be non-empty")
    momentum_version = _required(payload, "momentum_version", "truth set")
    if not isinstance(momentum_version, int) or isinstance(momentum_version, bool):
        raise ValueError("truth set momentum_version must be an integer")
    truth_thresholds = _required(payload, "thresholds", "truth set")
    if not isinstance(truth_thresholds, dict) or not truth_thresholds:
        raise ValueError("truth set thresholds must be a non-empty object")
    if set(truth_thresholds) != THRESHOLD_FIELDS:
        raise ValueError("truth thresholds must have the exact RTH threshold fields")
    for key in (
        "move_pct",
        "minimum_cumulative_notional",
        "maximum_data_age_seconds",
    ):
        if _finite(truth_thresholds[key], f"truth thresholds.{key}") <= 0:
            raise ValueError(f"truth thresholds.{key} must be positive")
    for key in ("persistence_bars", "window_lead_minutes"):
        value = truth_thresholds[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"truth thresholds.{key} must be a positive integer")
    bar_timeframe = truth_thresholds["bar_timeframe"]
    if not isinstance(bar_timeframe, str) or not bar_timeframe:
        raise ValueError("truth thresholds.bar_timeframe must be non-empty")
    cases_raw = _required(payload, "cases", "truth set")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError("truth set cases must be a non-empty list")
    cases = tuple(_parse_case(raw, index) for index, raw in enumerate(cases_raw))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("truth set case ids must be unique")
    tuning = {case.symbol for case in cases if case.cohort == "tuning"}
    evaluation = {case.symbol for case in cases if case.cohort != "tuning"}
    overlap = tuning & evaluation
    if overlap:
        raise ValueError(f"tuning and holdout symbols must be disjoint: {sorted(overlap)}")
    return version, momentum_version, truth_thresholds, cases


def _bar(case: RthTruthCase, raw: dict[str, Any], index: int) -> Bar:
    context = f"{case.case_id}.bars[{index}]"
    offset = _finite(_required(raw, "offset_min", context), f"{context}.offset_min")
    close = _finite(_required(raw, "close", context), f"{context}.close")
    open_ = _finite(raw.get("open", close), f"{context}.open")
    high = _finite(raw.get("high", max(open_, close)), f"{context}.high")
    low = _finite(raw.get("low", min(open_, close)), f"{context}.low")
    volume = _required(raw, "volume", context)
    if not isinstance(volume, int) or isinstance(volume, bool):
        raise ValueError(f"{context}.volume must be an integer")
    timestamp = case.session_close + timedelta(minutes=offset)
    if case.fault == "naive_timestamp" and index == len(case.bars) - 1:
        timestamp = timestamp.replace(tzinfo=None)
    return Bar(case.symbol, timestamp, open_, high, low, close, volume)


def replay_rth_case(case: RthTruthCase) -> RthReplayCaseResult:
    bars = tuple(_bar(case, raw, index) for index, raw in enumerate(case.bars))
    daily = () if case.prior_close is None else (
        Bar(
            case.symbol,
            case.session_open - timedelta(days=1),
            case.prior_close,
            case.prior_close,
            case.prior_close,
            case.prior_close,
            1_000_000,
        ),
    )
    evaluations = [
        (
            offset,
            evaluate_rth_momentum(
                case.symbol,
                case.event_date,
                bars,
                daily,
                session_open=case.session_open,
                session_close=case.session_close,
                now=case.session_close + timedelta(minutes=offset),
            ),
        )
        for offset in case.evaluation_offsets_min
    ]
    candidates = [
        (offset, result)
        for offset, result in evaluations
        if result.outcome == OUTCOME_CANDIDATE
    ]
    observed = bool(candidates)
    first = candidates[0][0] if candidates else None
    unique = len(
        {(case.event_date, case.symbol, row.direction) for _, row in candidates}
    )
    latency = (
        (first - case.eligible_offset_min) * 60
        if first is not None and case.eligible_offset_min is not None
        else None
    )
    final = evaluations[-1][1].outcome
    candidate_match = True if case.expected_candidate is None else observed == case.expected_candidate
    first_match = (
        case.expected_first_candidate_offset_min is None
        or first == case.expected_first_candidate_offset_min
    )
    passed = candidate_match and first_match and final == case.expected_final_outcome
    return RthReplayCaseResult(
        case_id=case.case_id,
        cohort=case.cohort,
        label=case.label,
        symbol=case.symbol,
        provenance=case.provenance,
        expected_candidate=case.expected_candidate,
        observed_candidate=observed,
        expected_final_outcome=case.expected_final_outcome,
        final_outcome=final,
        expected_first_candidate_offset_min=case.expected_first_candidate_offset_min,
        first_candidate_offset_min=first,
        detection_latency_seconds=latency,
        candidate_observations=len(candidates),
        unique_candidates=unique,
        duplicate_candidate_observations=max(0, len(candidates) - unique),
        passed=passed,
    )


def run_rth_replay(
    path: Path | str = DEFAULT_TRUTH_PATH,
    *,
    cohort: str = "contract_holdout",
) -> RthReplayReport:
    if cohort not in ALLOWED_COHORTS:
        raise ValueError(f"cohort must be one of {sorted(ALLOWED_COHORTS)}")
    version, truth_momentum_version, truth_thresholds, cases = load_rth_truth_set(path)
    selected = tuple(case for case in cases if case.cohort == cohort)
    if not selected:
        raise ValueError(f"truth set contains no {cohort!r} cases")
    results = tuple(replay_rth_case(case) for case in selected)
    definitive = tuple(row for row in results if row.expected_candidate is not None)
    tp = sum(row.expected_candidate is True and row.observed_candidate for row in definitive)
    fn = sum(row.expected_candidate is True and not row.observed_candidate for row in definitive)
    tn = sum(row.expected_candidate is False and not row.observed_candidate for row in definitive)
    fp = sum(row.expected_candidate is False and row.observed_candidate for row in definitive)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    latencies = [
        row.detection_latency_seconds
        for row in results
        if row.detection_latency_seconds is not None
    ]
    quality_leaks = sum(
        row.label == "negative"
        and row.observed_candidate
        and row.final_outcome in {"INVALID_DATA", "STALE", "BAR_GAP"}
        for row in results
    )
    metrics = RthReplayMetrics(
        definitive_cases=len(definitive),
        ambiguous_cases=len(results) - len(definitive),
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        exact_outcome_accuracy=(
            sum(row.final_outcome == row.expected_final_outcome for row in results)
            / len(results)
        ),
        cases_passed=sum(row.passed for row in results),
        cases_failed=sum(not row.passed for row in results),
        candidate_observations=sum(row.candidate_observations for row in results),
        unique_candidates=sum(row.unique_candidates for row in results),
        duplicate_candidate_observations=sum(
            row.duplicate_candidate_observations for row in results
        ),
        data_quality_candidate_leaks=quality_leaks,
        mean_detection_latency_seconds=(sum(latencies) / len(latencies) if latencies else None),
        max_detection_latency_seconds=max(latencies) if latencies else None,
    )
    positives = sum(row.expected_candidate is True for row in definitive)
    negatives = sum(row.expected_candidate is False for row in definitive)
    baseline = RthReplayBaseline(
        name="no_final_rth_handoff_lane",
        true_positives=0,
        false_positives=0,
        true_negatives=negatives,
        false_negatives=positives,
        precision=None,
        recall=0.0 if positives else None,
    )
    return RthReplayReport(
        truth_set_version=version,
        schema_version=TRUTH_SCHEMA_VERSION,
        truth_momentum_version=truth_momentum_version,
        momentum_version=RTH_MOMENTUM_VERSION,
        cohort=cohort,
        truth_thresholds=truth_thresholds,
        thresholds=rth_thresholds(),
        contract_compatible=(
            truth_momentum_version == RTH_MOMENTUM_VERSION
            and truth_thresholds == rth_thresholds()
        ),
        baseline=baseline,
        metrics=metrics,
        cases=results,
        evidence_boundary=(
            "Synthetic contract replay and named tuning regressions do not establish "
            "live precision, recall, profitability, or customer readiness."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("truth", nargs="?", type=Path, default=DEFAULT_TRUTH_PATH)
    parser.add_argument(
        "--cohort",
        choices=sorted(ALLOWED_COHORTS),
        default="contract_holdout",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_rth_replay(args.truth, cohort=args.cohort)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps(asdict(report), indent=None if args.compact else 2, sort_keys=True))
    return (
        0
        if report.metrics.cases_failed == 0 and report.contract_compatible
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
