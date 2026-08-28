#!/usr/bin/env python3
"""Operate locked, blinded postmarket rank experiments."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.journal import code_version, new_run_id
from tradebot.postmarket_empirical import (
    EligibilityRule,
    ExperimentPolicy,
    SelectionRule,
    create_locked_experiment,
    evaluate_rank_experiment,
    export_empirical_report,
    holdout_label_inventory,
    unblind_holdout,
)
from tradebot.postmarket_label_manifest import ingest_label_manifest


def _sessions(values: list[str]) -> tuple[date, ...]:
    return tuple(date.fromisoformat(value) for value in values)


def _json(value) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, default=Path("data/postmarket_shadow.db"))
    commands = root.add_subparsers(dest="command", required=True)

    lock = commands.add_parser("lock", help="create an immutable experiment")
    lock.add_argument("--experiment-id", required=True)
    lock.add_argument("--created-by", required=True)
    lock.add_argument("--rank-version", type=int, required=True)
    lock.add_argument("--label-method", required=True)
    lock.add_argument("--development-session", action="append", required=True)
    lock.add_argument("--holdout-session", action="append", required=True)
    lock.add_argument("--eligibility-move-pct", type=float, required=True)
    lock.add_argument("--eligibility-min-notional", type=float, required=True)
    lock.add_argument("--eligibility-persistence-bars", type=int, required=True)
    lock.add_argument("--minimum-evidence-score", type=float, required=True)
    lock.add_argument("--maximum-ordinal-rank", type=int)
    lock.add_argument("--min-precision", type=float, required=True)
    lock.add_argument("--min-recall", type=float, required=True)
    lock.add_argument("--min-definitive-labels", type=int, required=True)
    lock.add_argument("--min-positive-labels", type=int, required=True)

    labels = commands.add_parser("import-labels", help="ingest one blinded manifest")
    labels.add_argument("manifest", type=Path)
    labels.add_argument("--experiment-id", required=True)

    inventory = commands.add_parser("inventory", help="preview holdout freeze digest")
    inventory.add_argument("--experiment-id", required=True)

    unblind = commands.add_parser("unblind", help="irreversibly freeze and unblind holdout")
    unblind.add_argument("--experiment-id", required=True)
    unblind.add_argument("--unblinded-by", required=True)
    unblind.add_argument("--reason", required=True)
    unblind.add_argument("--expected-inventory-sha256", required=True)

    evaluate = commands.add_parser("evaluate", help="persist an immutable empirical report")
    evaluate.add_argument("--experiment-id", required=True)
    evaluate.add_argument("--split", choices=("development", "holdout"), required=True)
    evaluate.add_argument(
        "--output-dir", type=Path, default=Path("data/postmarket_audits")
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(args.db)
    try:
        if args.command == "lock":
            digest = create_locked_experiment(
                conn,
                experiment_id=args.experiment_id,
                created_at=now,
                created_by=args.created_by,
                rank_version=args.rank_version,
                label_method=args.label_method,
                development_sessions=_sessions(args.development_session),
                holdout_sessions=_sessions(args.holdout_session),
                eligibility_rule=EligibilityRule(
                    args.eligibility_move_pct,
                    args.eligibility_min_notional,
                    args.eligibility_persistence_bars,
                ),
                selection_rule=SelectionRule(
                    args.minimum_evidence_score, args.maximum_ordinal_rank,
                ),
                policy=ExperimentPolicy(
                    args.min_precision, args.min_recall,
                    args.min_definitive_labels, args.min_positive_labels,
                ),
            )
            _json({"experiment_id": args.experiment_id, "manifest_sha256": digest})
        elif args.command == "import-labels":
            manifest_id, created, labels, manifest = ingest_label_manifest(
                conn, args.manifest, experiment_id=args.experiment_id,
                observed_at=now, code_version=code_version(), run_id=new_run_id(),
            )
            _json({
                "label_manifest_id": manifest_id,
                "created": created,
                "labels_written": labels,
                "session": manifest.session.isoformat(),
                "manifest_sha256": manifest.manifest_sha256,
            })
        elif args.command == "inventory":
            digest, labels, latest = holdout_label_inventory(conn, args.experiment_id)
            _json({
                "experiment_id": args.experiment_id,
                "holdout_labels": labels,
                "latest_label_at_utc": latest.isoformat(),
                "label_inventory_sha256": digest,
                "unblind_performed": False,
            })
        elif args.command == "unblind":
            digest = unblind_holdout(
                conn, experiment_id=args.experiment_id, unblinded_at=now,
                unblinded_by=args.unblinded_by, reason=args.reason,
                expected_inventory_sha256=args.expected_inventory_sha256,
            )
            _json({
                "experiment_id": args.experiment_id,
                "label_inventory_sha256": digest,
                "unblind_performed": True,
            })
        elif args.command == "evaluate":
            report = evaluate_rank_experiment(
                conn, experiment_id=args.experiment_id, split=args.split,
                evaluated_at=now, code_version=code_version(),
            )
            artifact = export_empirical_report(
                conn,
                experiment_id=args.experiment_id,
                split=args.split,
                input_digest_sha256=report.input_digest_sha256,
                output_dir=args.output_dir,
            )
            payload = asdict(report)
            payload["artifact"] = asdict(artifact)
            _json(payload)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
