"""Versioned postmarket truth-set and offline replay quality gates."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tradebot import postmarket
from tradebot.postmarket_replay import (
    DEFAULT_TRUTH_PATH,
    load_truth_set,
    main,
    run_replay,
)


def test_versioned_truth_set_keeps_tuning_and_holdout_disjoint():
    version, observer_version, truth_thresholds, cases = load_truth_set()

    assert version == "postmarket-earnings-v1"
    assert observer_version == 1
    assert truth_thresholds == postmarket.thresholds()
    tuning = {case.symbol for case in cases if case.cohort == "tuning"}
    holdout = {case.symbol for case in cases if case.cohort == "contract_holdout"}
    assert tuning == {"CRM", "CRWD", "OKTA"}
    assert tuning.isdisjoint(holdout)
    assert {case.label for case in cases} == {"positive", "negative", "ambiguous"}
    assert {case.failure_class for case in cases} == {"none", "data_quality", "provider"}


def test_holdout_compares_zero_recall_baseline_to_candidate_metrics():
    report = run_replay(cohort="contract_holdout")

    assert report.truth_observer_version == report.observer_version == 1
    assert report.truth_thresholds == report.thresholds
    assert report.baseline.name == "no_postmarket_observer"
    assert report.baseline.true_positives == 0
    assert report.baseline.false_negatives == 4
    assert report.baseline.recall == 0.0

    metrics = report.metrics
    assert len(report.cases) == 18
    assert metrics.cases_passed == 18
    assert metrics.cases_failed == 0
    assert (metrics.true_positives, metrics.false_negatives) == (4, 0)
    assert (metrics.true_negatives, metrics.false_positives) == (13, 0)
    assert metrics.precision == metrics.recall == 1.0
    assert metrics.exact_outcome_accuracy == 1.0
    assert metrics.data_quality_candidate_leaks == 0
    assert metrics.direction_changes == 0


def test_latency_repeat_observations_and_deduplicated_candidates_are_separate_metrics():
    report = run_replay(cohort="contract_holdout")
    metrics = report.metrics

    assert metrics.max_detection_latency_seconds == 60.0
    assert metrics.mean_detection_latency_seconds == 15.0
    assert metrics.raw_candidate_observations == 7
    assert metrics.unique_candidates == 5  # four positives plus one ambiguous boundary
    assert metrics.duplicate_candidate_observations == 2
    delayed = next(case for case in report.cases if case.symbol == "POLLDELAY")
    assert delayed.first_candidate_offset_min == 11
    assert delayed.detection_latency_seconds == 60


def test_degraded_cases_never_leak_a_candidate():
    report = run_replay(cohort="contract_holdout")
    degraded = [case for case in report.cases if case.failure_class != "none"]

    assert degraded
    assert all(case.observed_candidate is False for case in degraded)
    assert {case.final_outcome for case in degraded} == {
        "UNSTABLE_PRINT",
        "BAR_GAP",
        "ZERO_VOLUME",
        "DUPLICATE_TIMESTAMP",
        "OUT_OF_ORDER",
        "MALFORMED_BAR",
        "STALE",
        "NO_RTH_CLOSE",
        "FETCH_ERROR",
    }


def test_tuning_examples_are_reported_but_not_counted_as_holdout():
    tuning = run_replay(cohort="tuning")
    holdout = run_replay(cohort="contract_holdout")

    assert {case.symbol for case in tuning.cases} == {"CRM", "CRWD", "OKTA"}
    assert not ({case.symbol for case in tuning.cases} & {case.symbol for case in holdout.cases})
    assert tuning.metrics.cases_failed == 0


def test_threshold_drift_fails_the_locked_truth_contract(monkeypatch):
    monkeypatch.setattr(postmarket, "MOVE_THRESHOLD_PCT", 20.0)

    report = run_replay(cohort="contract_holdout")

    assert report.thresholds["move_pct"] == 20.0
    assert report.truth_thresholds["move_pct"] == 8.0
    assert report.metrics.cases_failed > 0
    assert report.metrics.recall < 1.0


def test_replay_module_has_no_live_or_delivery_imports():
    source_path = Path(__file__).parents[1] / "tradebot" / "postmarket_replay.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = (
        "requests",
        "tradebot.alerts",
        "tradebot.journal",
        "tradebot.marketdata",
        "tradebot.telegram_bot",
        "tradebot.vendors",
    )
    assert not any(module.startswith(forbidden) for module in imports)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"schema_version": 2}, "unsupported truth schema"),
        ({"observer_version": True}, "observer_version must be an integer"),
        ({"thresholds": {}}, "thresholds must be a non-empty object"),
    ],
)
def test_truth_metadata_fails_closed(tmp_path, mutation, expected):
    payload = json.loads(DEFAULT_TRUTH_PATH.read_text(encoding="utf-8"))
    payload.update(mutation)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        load_truth_set(path)


@pytest.mark.parametrize(
    "thresholds, expected",
    [
        ({"move_pct": 8.0}, "thresholds must contain exactly"),
        (
            {
                "move_pct": True,
                "min_cumulative_notional": 100_000.0,
                "persistence_bars": 2,
                "max_close_divergence_pct": 10.0,
                "max_data_age_seconds": 420,
            },
            "move_pct must be numeric",
        ),
    ],
)
def test_truth_threshold_snapshot_is_strictly_typed(tmp_path, thresholds, expected):
    payload = json.loads(DEFAULT_TRUTH_PATH.read_text(encoding="utf-8"))
    payload["thresholds"] = thresholds
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        load_truth_set(path)


def test_truth_case_requires_timezone_aware_session_close(tmp_path):
    payload = json.loads(DEFAULT_TRUTH_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["session_close_utc"] = "2026-08-26T20:00:00"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="timezone-aware"):
        load_truth_set(path)


def test_empirical_holdout_is_explicitly_empty_until_real_cases_are_labeled():
    with pytest.raises(ValueError, match="no 'empirical_holdout' cases"):
        run_replay(cohort="empirical_holdout")


def test_cli_emits_machine_readable_report_and_uses_exit_status(capsys):
    exit_code = main(["--cohort", "contract_holdout", "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["truth_set_version"] == "postmarket-earnings-v1"
    assert payload["cohort"] == "contract_holdout"
    assert payload["metrics"]["cases_failed"] == 0


def test_cli_returns_configuration_error_for_missing_file(tmp_path, capsys):
    exit_code = main([str(tmp_path / "missing.json"), "--compact"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"].startswith("FileNotFoundError:")
