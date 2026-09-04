"""Provider-neutral licensed-reference manifest builder tests."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

from tradebot.postmarket_reference_manifest_builder import (
    CSV_FIELDS,
    build_reference_manifest,
    write_reference_manifest_exclusive,
)


PUBLISHED = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
CREATED = datetime(2026, 9, 2, 20, 5, tzinfo=timezone.utc)


def _write_csv(path, rows=None, header=CSV_FIELDS):
    if rows is None:
        rows = [
            ("XYZ", "40", "Financials", "XLF", "", ""),
            (
                "ABC",
                "45",
                "Information Technology",
                "XLK",
                "1000000",
                "2026-09-01",
            ),
        ]
    lines = [",".join(header), *(",".join(row) for row in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build(path):
    return build_reference_manifest(
        path,
        provider="licensed-vendor",
        dataset="sector-export-v1",
        license_reference="agreement-2026-09",
        effective_date=date(2026, 9, 2),
        published_at_utc=PUBLISHED,
        created_at_utc=CREATED,
        classification_system="GICS",
    )


def test_build_is_canonical_deterministic_and_parser_validated(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    rows = [
        ("XYZ", "40", "Financials", "XLF", "", ""),
        ("ABC", "45", "Information Technology", "XLK", "1000000", "2026-09-01"),
    ]
    _write_csv(first, rows)
    _write_csv(second, list(reversed(rows)))
    built = _build(first)
    assert built.raw == _build(second).raw
    assert [row.symbol for row in built.manifest.rows] == ["ABC", "XYZ"]
    assert built.manifest.rows[0].float_shares == 1_000_000
    assert built.manifest.rows[1].float_shares is None


@pytest.mark.parametrize(
    ("header", "rows", "message"),
    [
        (CSV_FIELDS + ("extra",), None, "header must exactly"),
        (("symbol", "symbol", *CSV_FIELDS[2:]), None, "duplicate CSV headers"),
        (CSV_FIELDS, [("abc", "45", "Technology", "XLK", "", "")], "canonical uppercase"),
        (CSV_FIELDS, [("ABC", "45", "Technology", "SPY", "", "")], "Select Sector"),
        (CSV_FIELDS, [("ABC", "45", "Technology", "XLK", "10", "")], "supplied together"),
        (
            CSV_FIELDS,
            [("ABC", "45", "Technology", "XLK", "nan", "2026-09-01")],
            "finite and positive",
        ),
    ],
)
def test_build_rejects_ambiguous_or_incomplete_exports(tmp_path, header, rows, message):
    source = tmp_path / "source.csv"
    _write_csv(source, rows=rows, header=header)
    with pytest.raises(ValueError, match=message):
        _build(source)


def test_build_rejects_duplicates_guessed_license_and_symlink(tmp_path):
    source = tmp_path / "source.csv"
    _write_csv(source, rows=[
        ("ABC", "45", "Technology", "XLK", "", ""),
        ("ABC", "45", "Technology", "XLK", "", ""),
    ])
    with pytest.raises(ValueError, match="duplicate symbol"):
        _build(source)

    valid = tmp_path / "valid.csv"
    _write_csv(valid)
    link = tmp_path / "link.csv"
    link.symlink_to(valid)
    with pytest.raises(ValueError, match="non-symlink"):
        _build(link)
    with pytest.raises(ValueError, match="operator-reviewed"):
        build_reference_manifest(
            valid,
            provider="vendor",
            dataset="dataset",
            license_reference="unlicensed",
            effective_date=date(2026, 9, 2),
            published_at_utc=PUBLISHED,
            created_at_utc=CREATED,
            classification_system="GICS",
        )


def test_exclusive_publication_is_read_only_and_never_overwrites(tmp_path):
    source = tmp_path / "source.csv"
    destination = tmp_path / "manifest.json"
    _write_csv(source)
    built = _build(source)
    write_reference_manifest_exclusive(destination, built)
    assert destination.read_bytes() == built.raw
    assert os.stat(destination).st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError, match="already exists"):
        write_reference_manifest_exclusive(destination, built)
    assert destination.read_bytes() == built.raw
