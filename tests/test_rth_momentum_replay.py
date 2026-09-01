"""Versioned replay contract for final-RTH momentum evidence."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tradebot import rth_momentum
from tradebot.rth_momentum_replay import (
    DEFAULT_TRUTH_PATH,
    load_rth_truth_set,
    main,
    run_rth_replay,
)


def _payload() -> dict:
    return json.loads(DEFAULT_TRUTH_PATH.read_text(encoding="utf-8"))


def _write_payload(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "truth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_truth_cohorts_are_disjoint_and_gpro_is_tuning_only():
    version, momentum_version, thresholds, cases = load_rth_truth_set()
    tuning = {case.symbol for case in cases if case.cohort == "tuning"}
    holdout = {
        case.symbol for case in cases if case.cohort == "contract_holdout"
    }

    assert version == "rth-final-handoff-v1"
    assert momentum_version == 1
    assert thresholds == rth_momentum.rth_thresholds()
    assert tuning == {"GPRO"}
    assert "GPRO" not in holdout
    assert tuning.isdisjoint(holdout)


def test_contract_holdout_passes_with_explicit_quality_outcomes():
    report = run_rth_replay()
    outcomes = {row.final_outcome for row in report.cases}

    assert report.cohort == "contract_holdout"
    assert len(report.cases) == 16
    assert report.metrics.true_positives == 4
    assert report.metrics.false_positives == 0
    assert report.metrics.true_negatives == 12
    assert report.metrics.false_negatives == 0
    assert report.metrics.precision == 1.0
    assert report.metrics.recall == 1.0
    assert report.metrics.exact_outcome_accuracy == 1.0
    assert report.metrics.cases_failed == 0
    assert report.metrics.data_quality_candidate_leaks == 0
    assert report.contract_compatible is True
    assert report.baseline.false_negatives == 4
    assert report.baseline.recall == 0.0
    assert {
        "INVALID_DATA",
        "BAR_GAP",
        "STALE",
        "NO_PRIOR_CLOSE",
        "NO_COMPLETED_RTH_BAR",
    } <= outcomes
    assert "do not establish" in report.evidence_boundary


def test_named_gpro_regression_passes_without_becoming_holdout_evidence():
    report = run_rth_replay(cohort="tuning")

    assert len(report.cases) == 1
    case = report.cases[0]
    assert case.symbol == "GPRO"
    assert case.first_candidate_offset_min == 0
    assert case.detection_latency_seconds == 0
    assert case.passed is True


def test_threshold_drift_is_visible_and_breaks_the_holdout(monkeypatch):
    monkeypatch.setattr(rth_momentum, "MOVE_THRESHOLD_PCT", 60.0)

    report = run_rth_replay()

    assert report.truth_thresholds["move_pct"] == 8.0
    assert report.thresholds["move_pct"] == 60.0
    assert report.contract_compatible is False
    assert report.metrics.false_negatives == 4
    assert report.metrics.recall == 0.0
    assert report.metrics.cases_failed >= 4


def test_empirical_holdout_is_intentionally_empty_and_fails_closed():
    with pytest.raises(
        ValueError,
        match="contains no 'empirical_holdout' cases",
    ):
        run_rth_replay(cohort="empirical_holdout")


def test_non_case_breaking_threshold_drift_still_fails_cli(monkeypatch, capsys):
    monkeypatch.setattr(rth_momentum, "MAX_DATA_AGE_SECONDS", 421)

    report = run_rth_replay()
    assert report.metrics.cases_failed == 0
    assert report.contract_compatible is False
    assert main(["--compact"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_compatible"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("move_pct", "8", "must be numeric"),
        ("persistence_bars", 2.5, "positive integer"),
        ("bar_timeframe", "", "must be non-empty"),
    ),
)
def test_truth_threshold_types_are_strict(tmp_path, field, value, message):
    payload = _payload()
    payload["thresholds"][field] = value

    with pytest.raises(ValueError, match=message):
        load_rth_truth_set(_write_payload(tmp_path, payload))


def test_truth_rejects_wrong_schema(tmp_path):
    payload = _payload()
    payload["schema_version"] = 2

    with pytest.raises(ValueError, match="unsupported RTH truth schema"):
        load_rth_truth_set(_write_payload(tmp_path, payload))


def test_truth_rejects_naive_or_incorrect_exchange_bounds(tmp_path):
    payload = _payload()
    payload["cases"][0]["session_close_utc"] = "2026-08-31T20:00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        load_rth_truth_set(_write_payload(tmp_path, payload))

    payload = _payload()
    payload["cases"][0]["session_close_utc"] = "2026-08-31T21:00:00+00:00"
    with pytest.raises(ValueError, match="do not match XNYS"):
        load_rth_truth_set(_write_payload(tmp_path, payload))


def test_truth_rejects_tuning_holdout_symbol_overlap(tmp_path):
    payload = _payload()
    payload["cases"][1]["symbol"] = "GPRO"

    with pytest.raises(ValueError, match="must be disjoint"):
        load_rth_truth_set(_write_payload(tmp_path, payload))


def test_replay_module_has_no_live_provider_delivery_or_persistence_import():
    tree = ast.parse(
        Path(__file__).parents[1]
        .joinpath("tradebot", "rth_momentum_replay.py")
        .read_text(encoding="utf-8")
    )
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert not any(
        module.startswith(
            (
                "tradebot.vendors",
                "tradebot.telegram",
                "tradebot.alert",
                "tradebot.journal",
            )
        )
        for module in modules
    )


def test_cli_returns_pass_failure_and_configuration_codes(
    tmp_path,
    capsys,
    monkeypatch,
):
    assert main(["--compact"]) == 0
    passed = json.loads(capsys.readouterr().out)
    assert passed["metrics"]["cases_failed"] == 0

    monkeypatch.setattr(rth_momentum, "MOVE_THRESHOLD_PCT", 60.0)
    assert main(["--compact"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["metrics"]["cases_failed"] >= 4

    assert main([str(tmp_path / "missing.json"), "--compact"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"].startswith("FileNotFoundError:")
