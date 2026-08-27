"""Offline quality replay for versioned postmarket truth sets.

The harness imports the same pure evaluator used by the shadow service but
has no live market-data, journal, alert, Telegram, or broker dependency. A
truth case declares exactly which evaluation instants existed; this keeps a
stale-start or provider-failure fixture from accidentally gaining earlier,
unavailable observations during replay.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from tradebot.detectors import Bar
from tradebot.postmarket import (
    OBSERVER_VERSION,
    OUTCOME_CANDIDATE,
    evaluate_earnings_reaction,
    fetch_error_evaluation,
    thresholds,
)


TRUTH_SCHEMA_VERSION = 1
DEFAULT_TRUTH_PATH = (
    Path(__file__).resolve().parent.parent / "truth" / "postmarket_earnings_v1.json"
)
ALLOWED_COHORTS = {"tuning", "contract_holdout", "empirical_holdout"}
ALLOWED_LABELS = {"positive", "negative", "ambiguous"}
ALLOWED_FAILURE_CLASSES = {"none", "data_quality", "provider"}
THRESHOLD_FIELDS = {
    "move_pct",
    "min_cumulative_notional",
    "persistence_bars",
    "max_close_divergence_pct",
    "max_data_age_seconds",
}


@dataclass(frozen=True)
class TruthCase:
    case_id: str
    cohort: str
    label: str
    symbol: str
    event_date: date
    session_close: datetime
    evaluation_offsets_min: tuple[float, ...]
    expected_candidate: bool | None
    expected_final_outcome: str
    expected_first_candidate_offset_min: float | None
    eligible_offset_min: float | None
    failure_class: str
    provenance: str
    notes: str
    rth_close: float | None
    rth_offset_min: float
    rth_bar: dict[str, Any] | None
    postmarket_bars: tuple[dict[str, Any], ...]
    fault: str | None


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    cohort: str
    label: str
    symbol: str
    provenance: str
    failure_class: str
    expected_candidate: bool | None
    observed_candidate: bool
    expected_final_outcome: str
    final_outcome: str
    expected_first_candidate_offset_min: float | None
    first_candidate_offset_min: float | None
    detection_latency_seconds: float | None
    raw_candidate_observations: int
    unique_candidates: int
    duplicate_candidate_observations: int
    direction_changes: int
    passed: bool


@dataclass(frozen=True)
class ReplayMetrics:
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
    raw_candidate_observations: int
    unique_candidates: int
    duplicate_candidate_observations: int
    direction_changes: int
    data_quality_candidate_leaks: int
    mean_detection_latency_seconds: float | None
    max_detection_latency_seconds: float | None


@dataclass(frozen=True)
class BaselineMetrics:
    name: str
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True)
class ReplayReport:
    truth_set_version: str
    schema_version: int
    truth_observer_version: int
    observer_version: int
    cohort: str
    truth_thresholds: dict[str, Any]
    thresholds: dict[str, Any]
    baseline: BaselineMetrics
    metrics: ReplayMetrics
    cases: tuple[CaseResult, ...]


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _aware_datetime(raw: Any, context: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must be an ISO-8601 string")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value


def _finite_number(raw: Any, context: str) -> float:
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError(f"{context} must be numeric")
    value = float(raw)
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError(f"{context} must be finite")
    return value


def _parse_case(raw: Any, index: int) -> TruthCase:
    context = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    case_id = _required(raw, "id", context)
    cohort = _required(raw, "cohort", context)
    label = _required(raw, "label", context)
    failure_class = raw.get("failure_class", "none")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"{context}.id must be a non-empty string")
    if cohort not in ALLOWED_COHORTS:
        raise ValueError(f"{context}.cohort must be one of {sorted(ALLOWED_COHORTS)}")
    if label not in ALLOWED_LABELS:
        raise ValueError(f"{context}.label must be one of {sorted(ALLOWED_LABELS)}")
    if failure_class not in ALLOWED_FAILURE_CLASSES:
        raise ValueError(
            f"{context}.failure_class must be one of {sorted(ALLOWED_FAILURE_CLASSES)}"
        )

    symbol = _required(raw, "symbol", context)
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"{context}.symbol must be non-empty")
    session_close = _aware_datetime(
        _required(raw, "session_close_utc", context), f"{context}.session_close_utc"
    )
    event_date_raw = _required(raw, "event_date", context)
    if not isinstance(event_date_raw, str):
        raise ValueError(f"{context}.event_date must be an ISO date string")
    try:
        event_date = date.fromisoformat(event_date_raw)
    except ValueError as exc:
        raise ValueError(f"{context}.event_date must be an ISO date string") from exc
    if session_close.utcoffset() != timedelta(0):
        raise ValueError(f"{context}.session_close_utc must use a UTC offset")
    if session_close.date() != event_date:
        raise ValueError(f"{context}.session_close_utc must fall on event_date")
    offsets_raw = _required(raw, "evaluation_offsets_min", context)
    if not isinstance(offsets_raw, list) or not offsets_raw:
        raise ValueError(f"{context}.evaluation_offsets_min must be a non-empty list")
    offsets = tuple(
        _finite_number(value, f"{context}.evaluation_offsets_min") for value in offsets_raw
    )
    if list(offsets) != sorted(offsets) or len(offsets) != len(set(offsets)):
        raise ValueError(f"{context}.evaluation_offsets_min must be unique and ordered")
    if offsets[0] < 0:
        raise ValueError(f"{context}.evaluation_offsets_min must be non-negative")

    expected_candidate = _required(raw, "expected_candidate", context)
    if expected_candidate is not None and not isinstance(expected_candidate, bool):
        raise ValueError(f"{context}.expected_candidate must be true, false, or null")
    if label == "positive" and expected_candidate is not True:
        raise ValueError(f"{context} positive labels must expect a candidate")
    if label == "negative" and expected_candidate is not False:
        raise ValueError(f"{context} negative labels must reject a candidate")
    if label == "ambiguous" and expected_candidate is not None:
        raise ValueError(f"{context} ambiguous labels must use null expected_candidate")

    first_offset_raw = raw.get("expected_first_candidate_offset_min")
    first_offset = (
        _finite_number(first_offset_raw, f"{context}.expected_first_candidate_offset_min")
        if first_offset_raw is not None
        else None
    )
    eligible_raw = raw.get("eligible_offset_min")
    eligible_offset = (
        _finite_number(eligible_raw, f"{context}.eligible_offset_min")
        if eligible_raw is not None
        else None
    )
    if expected_candidate is True and (first_offset is None or eligible_offset is None):
        raise ValueError(f"{context} candidates require first-candidate and eligible offsets")
    if first_offset is not None and first_offset not in offsets:
        raise ValueError(f"{context}.expected_first_candidate_offset_min must be evaluated")
    if first_offset is not None and eligible_offset is not None and eligible_offset > first_offset:
        raise ValueError(f"{context}.eligible_offset_min cannot follow first detection")

    rth_close_raw = raw.get("rth_close")
    rth_close = (
        _finite_number(rth_close_raw, f"{context}.rth_close")
        if rth_close_raw is not None
        else None
    )
    rth_offset = _finite_number(raw.get("rth_offset_min", -5), f"{context}.rth_offset_min")
    rth_bar = raw.get("rth_bar")
    if rth_bar is not None and not isinstance(rth_bar, dict):
        raise ValueError(f"{context}.rth_bar must be an object")
    bars_raw = raw.get("postmarket_bars", [])
    if not isinstance(bars_raw, list) or any(not isinstance(bar, dict) for bar in bars_raw):
        raise ValueError(f"{context}.postmarket_bars must be a list of objects")
    fault = raw.get("fault")
    if fault not in {None, "fetch_error"}:
        raise ValueError(f"{context}.fault must be null or 'fetch_error'")
    if fault == "fetch_error" and failure_class != "provider":
        raise ValueError(f"{context}.fault fetch_error requires provider failure_class")

    expected_outcome = _required(raw, "expected_final_outcome", context)
    if not isinstance(expected_outcome, str) or not expected_outcome:
        raise ValueError(f"{context}.expected_final_outcome must be non-empty")
    provenance = _required(raw, "provenance", context)
    notes = _required(raw, "notes", context)
    if not isinstance(provenance, str) or not provenance:
        raise ValueError(f"{context}.provenance must be non-empty")
    if not isinstance(notes, str) or not notes:
        raise ValueError(f"{context}.notes must be non-empty")

    return TruthCase(
        case_id=case_id,
        cohort=cohort,
        label=label,
        symbol=symbol.strip().upper(),
        event_date=event_date,
        session_close=session_close,
        evaluation_offsets_min=offsets,
        expected_candidate=expected_candidate,
        expected_final_outcome=expected_outcome,
        expected_first_candidate_offset_min=first_offset,
        eligible_offset_min=eligible_offset,
        failure_class=failure_class,
        provenance=provenance,
        notes=notes,
        rth_close=rth_close,
        rth_offset_min=rth_offset,
        rth_bar=rth_bar,
        postmarket_bars=tuple(bars_raw),
        fault=fault,
    )


def load_truth_set(
    path: Path | str = DEFAULT_TRUTH_PATH,
) -> tuple[str, int, dict[str, Any], tuple[TruthCase, ...]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("truth set root must be an object")
    schema_version = _required(payload, "schema_version", "truth set")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != TRUTH_SCHEMA_VERSION
    ):
        raise ValueError(
            f"unsupported truth schema version {schema_version!r}; expected {TRUTH_SCHEMA_VERSION}"
        )
    version = _required(payload, "truth_set_version", "truth set")
    if not isinstance(version, str) or not version:
        raise ValueError("truth_set_version must be non-empty")
    truth_observer_version = _required(payload, "observer_version", "truth set")
    if not isinstance(truth_observer_version, int) or isinstance(truth_observer_version, bool):
        raise ValueError("truth set observer_version must be an integer")
    truth_thresholds = _required(payload, "thresholds", "truth set")
    if not isinstance(truth_thresholds, dict) or not truth_thresholds:
        raise ValueError("truth set thresholds must be a non-empty object")
    missing_thresholds = THRESHOLD_FIELDS - truth_thresholds.keys()
    extra_thresholds = truth_thresholds.keys() - THRESHOLD_FIELDS
    if missing_thresholds or extra_thresholds:
        raise ValueError(
            "truth set thresholds must contain exactly "
            f"{sorted(THRESHOLD_FIELDS)}; missing={sorted(missing_thresholds)} "
            f"extra={sorted(extra_thresholds)}"
        )
    for key in THRESHOLD_FIELDS - {"persistence_bars"}:
        if _finite_number(truth_thresholds[key], f"truth set thresholds.{key}") <= 0:
            raise ValueError(f"truth set thresholds.{key} must be positive")
    persistence = truth_thresholds["persistence_bars"]
    if not isinstance(persistence, int) or isinstance(persistence, bool) or persistence < 2:
        raise ValueError("truth set thresholds.persistence_bars must be an integer >= 2")
    raw_cases = _required(payload, "cases", "truth set")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("truth set cases must be a non-empty list")
    cases = tuple(_parse_case(raw, index) for index, raw in enumerate(raw_cases))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("truth set case ids must be unique")
    tuning_symbols = {case.symbol for case in cases if case.cohort == "tuning"}
    evaluation_symbols = {case.symbol for case in cases if case.cohort != "tuning"}
    overlap = tuning_symbols & evaluation_symbols
    if overlap:
        raise ValueError(f"tuning and holdout symbols must be disjoint: {sorted(overlap)}")
    return version, truth_observer_version, truth_thresholds, cases


def _bar(symbol: str, session_close: datetime, raw: dict[str, Any]) -> Bar:
    offset = _finite_number(_required(raw, "offset_min", "bar"), "bar.offset_min")
    close = _finite_number(_required(raw, "close", "bar"), "bar.close")
    open_ = _finite_number(raw.get("open", close), "bar.open")
    high = _finite_number(raw.get("high", max(open_, close)), "bar.high")
    low = _finite_number(raw.get("low", min(open_, close)), "bar.low")
    volume_raw = _required(raw, "volume", "bar")
    if not isinstance(volume_raw, int) or isinstance(volume_raw, bool):
        raise ValueError("bar.volume must be an integer")
    return Bar(
        symbol=symbol,
        ts=session_close + timedelta(minutes=offset),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume_raw,
    )


def replay_case(case: TruthCase) -> CaseResult:
    rth_bars: tuple[Bar, ...]
    if case.rth_close is None:
        rth_bars = ()
    else:
        raw_rth = {
            "offset_min": case.rth_offset_min,
            "close": case.rth_close,
            "volume": 1_000_000,
            **(case.rth_bar or {}),
        }
        rth_bars = (_bar(case.symbol, case.session_close, raw_rth),)
    postmarket_bars = tuple(
        _bar(case.symbol, case.session_close, raw) for raw in case.postmarket_bars
    )

    evaluations = []
    for offset in case.evaluation_offsets_min:
        if case.fault == "fetch_error":
            evaluation = fetch_error_evaluation(
                case.symbol, case.event_date, RuntimeError("truth-set provider failure")
            )
        else:
            evaluation = evaluate_earnings_reaction(
                case.symbol,
                case.event_date,
                rth_bars,
                postmarket_bars,
                session_close=case.session_close,
                now=case.session_close + timedelta(minutes=offset),
            )
        evaluations.append((offset, evaluation))

    candidates = [
        (offset, result)
        for offset, result in evaluations
        if result.outcome == OUTCOME_CANDIDATE
    ]
    observed_candidate = bool(candidates)
    first_offset = candidates[0][0] if candidates else None
    directions = [result.direction for _, result in candidates]
    unique_keys = {(case.event_date, case.symbol, direction) for direction in directions}
    direction_changes = sum(
        previous != current for previous, current in zip(directions, directions[1:])
    )
    detection_latency = (
        (first_offset - case.eligible_offset_min) * 60
        if first_offset is not None and case.eligible_offset_min is not None
        else None
    )
    final_outcome = evaluations[-1][1].outcome
    candidate_matches = (
        True if case.expected_candidate is None else observed_candidate == case.expected_candidate
    )
    first_matches = (
        True
        if case.expected_first_candidate_offset_min is None
        else first_offset == case.expected_first_candidate_offset_min
    )
    passed = (
        candidate_matches
        and first_matches
        and final_outcome == case.expected_final_outcome
        and (detection_latency is None or detection_latency >= 0)
    )
    return CaseResult(
        case_id=case.case_id,
        cohort=case.cohort,
        label=case.label,
        symbol=case.symbol,
        provenance=case.provenance,
        failure_class=case.failure_class,
        expected_candidate=case.expected_candidate,
        observed_candidate=observed_candidate,
        expected_final_outcome=case.expected_final_outcome,
        final_outcome=final_outcome,
        expected_first_candidate_offset_min=case.expected_first_candidate_offset_min,
        first_candidate_offset_min=first_offset,
        detection_latency_seconds=detection_latency,
        raw_candidate_observations=len(candidates),
        unique_candidates=len(unique_keys),
        duplicate_candidate_observations=max(0, len(candidates) - len(unique_keys)),
        direction_changes=direction_changes,
        passed=passed,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(results: Iterable[CaseResult]) -> ReplayMetrics:
    rows = tuple(results)
    definitive = tuple(row for row in rows if row.label != "ambiguous")
    tp = sum(row.label == "positive" and row.observed_candidate for row in definitive)
    fn = sum(row.label == "positive" and not row.observed_candidate for row in definitive)
    fp = sum(row.label == "negative" and row.observed_candidate for row in definitive)
    tn = sum(row.label == "negative" and not row.observed_candidate for row in definitive)
    latencies = [
        row.detection_latency_seconds
        for row in rows
        if row.detection_latency_seconds is not None
    ]
    return ReplayMetrics(
        definitive_cases=len(definitive),
        ambiguous_cases=len(rows) - len(definitive),
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=_ratio(tp, tp + fp),
        recall=_ratio(tp, tp + fn),
        exact_outcome_accuracy=_ratio(
            sum(row.final_outcome == row.expected_final_outcome for row in rows), len(rows)
        ) or 0.0,
        cases_passed=sum(row.passed for row in rows),
        cases_failed=sum(not row.passed for row in rows),
        raw_candidate_observations=sum(row.raw_candidate_observations for row in rows),
        unique_candidates=sum(row.unique_candidates for row in rows),
        duplicate_candidate_observations=sum(
            row.duplicate_candidate_observations for row in rows
        ),
        direction_changes=sum(row.direction_changes for row in rows),
        data_quality_candidate_leaks=sum(
            row.failure_class != "none" and row.observed_candidate for row in rows
        ),
        mean_detection_latency_seconds=(sum(latencies) / len(latencies) if latencies else None),
        max_detection_latency_seconds=(max(latencies) if latencies else None),
    )


def _baseline_metrics(results: Iterable[CaseResult]) -> BaselineMetrics:
    """The shipped pre-observer baseline: no postmarket candidates at all."""
    definitive = tuple(row for row in results if row.label != "ambiguous")
    positives = sum(row.label == "positive" for row in definitive)
    negatives = sum(row.label == "negative" for row in definitive)
    return BaselineMetrics(
        name="no_postmarket_observer",
        true_positives=0,
        false_positives=0,
        true_negatives=negatives,
        false_negatives=positives,
        precision=None,
        recall=0.0 if positives else None,
    )


def run_replay(
    path: Path | str = DEFAULT_TRUTH_PATH, *, cohort: str = "contract_holdout"
) -> ReplayReport:
    allowed = {"tuning", "contract_holdout", "empirical_holdout", "all"}
    if cohort not in allowed:
        raise ValueError(f"cohort must be one of {sorted(allowed)}")
    version, truth_observer_version, truth_thresholds, cases = load_truth_set(path)
    selected = cases if cohort == "all" else tuple(case for case in cases if case.cohort == cohort)
    if not selected:
        raise ValueError(f"truth set contains no {cohort!r} cases")
    results = tuple(replay_case(case) for case in selected)
    return ReplayReport(
        truth_set_version=version,
        schema_version=TRUTH_SCHEMA_VERSION,
        truth_observer_version=truth_observer_version,
        observer_version=OBSERVER_VERSION,
        cohort=cohort,
        truth_thresholds=truth_thresholds,
        thresholds=thresholds(),
        baseline=_baseline_metrics(results),
        metrics=_metrics(results),
        cases=results,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("truth_set", nargs="?", type=Path, default=DEFAULT_TRUTH_PATH)
    parser.add_argument(
        "--cohort",
        choices=("tuning", "contract_holdout", "empirical_holdout", "all"),
        default="contract_holdout",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_replay(args.truth_set, cohort=args.cohort)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(
        json.dumps(
            asdict(report),
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    return 0 if report.metrics.cases_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
