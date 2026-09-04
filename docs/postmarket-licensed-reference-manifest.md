# Licensed point-in-time reference manifest

Perch accepts sector classification, sector-benchmark mapping, and float shares
only through an operator-reviewed, provider-authorized manifest. It does not
scrape an ETF sponsor, derive sector from SEC SIC, or treat SEC public-float
value as float shares. The import is evidence plumbing, not proof of a license:
the operator remains responsible for verifying that `license_reference`
identifies a real grant covering Perch's use.

Context version 2 may use an eligible row to fetch the mapped Select Sector ETF
through the existing SIP intraday path and record the candidate-minus-sector
move at the candidate's first knowable completed bar. The context row binds the
manifest ID, SHA-256 digest, and source observation time. A missing row, a row
published or observed after detection, or missing causal benchmark bars remains
explicitly unavailable; Perch does not fall back to an inferred sector.

These facts remain shadow evidence and are not rank-version-1 inputs. A later
scoring change still requires a locked walk-forward and holdout qualification.

## Version 1 contract

The input is UTF-8 JSON with exactly these root fields:

```json
{
  "schema_version": 1,
  "status": "locked",
  "provider": "licensed-vendor",
  "dataset": "daily-sector-and-float-v1",
  "license_reference": "contract-2026-001",
  "effective_date": "2026-08-27",
  "published_at_utc": "2026-08-27T18:00:00+00:00",
  "created_at_utc": "2026-08-27T18:01:00+00:00",
  "classification_system": "GICS",
  "rows": [
    {
      "symbol": "ABC",
      "sector_code": "45",
      "sector_name": "Information Technology",
      "benchmark_symbol": "XLK",
      "float_shares": 1000000,
      "float_as_of_date": "2026-08-26"
    }
  ]
}
```

`classification_system` must be `GICS`, `ICB`, or `PROVIDER_SECTOR`.
`benchmark_symbol` is limited to the eleven Select Sector SPDR symbols. Symbols
must be unique canonical uppercase. Float value and date must be supplied
together; an omitted float is represented by two JSON nulls. The manifest must
contain 1–20,000 rows and no unrecognized fields.

Publication must precede creation, which must precede Perch's observation of
the file. Float's as-of date cannot follow the manifest effective date. A row
can bind to a candidate only when its effective date is no later than the
candidate session and both publication and first observation happened no later
than candidate detection. This preserves replay causality.

## Import

### Build from a provider CSV export

Perch includes an offline, provider-neutral normalizer for exports with exactly
this header:

```text
symbol,sector_code,sector_name,benchmark_symbol,float_shares,float_as_of_date
```

Every row must provide an explicit, supported Select Sector ETF mapping. The
tool never guesses a sector or benchmark. Float value and as-of date must both
be present or both be blank. It canonicalizes row order, validates the finished
artifact through the same parser used by ingestion, and publishes it read-only
without overwriting an existing path:

```bash
python3 scripts/build_postmarket_reference_manifest.py \
  /secure/provider/reference-2026-09-02.csv \
  /secure/provider/reference-2026-09-02.json \
  --provider licensed-vendor \
  --dataset sector-export-v1 \
  --license-reference agreement-2026-09 \
  --effective-date 2026-09-02 \
  --published-at-utc 2026-09-02T20:00:00+00:00 \
  --classification-system GICS
```

The output summary contains only metadata, row count, and digest—not licensed
rows. This conversion is not license evidence: the operator must still verify
the agreement, publication time, field semantics, and permitted use before
import. The command performs no network requests and does not enable any
service or customer-delivery lane.

### Ingest the locked manifest

Place the provider-delivered file outside the repository, verify the license
and dates, then run:

```bash
python3 scripts/import_postmarket_reference_manifest.py \
  /secure/provider/reference-2026-08-27.json \
  --db data/postmarket_shadow.db
```

The command emits a safe summary containing identifiers and the SHA-256 digest,
not the licensed rows. The source must be a regular non-symlink file. Manifests
and rows are append-only; importing identical bytes is digest-idempotent.

After import, the isolated external-context worker records one
`LICENSED_POINT_IN_TIME_REFERENCE` fact for each eligible candidate. The fact
retains provider, dataset, license reference, effective/publication/observation
times, manifest digest, classification, benchmark, float value/date, and an
explicit not-inferred semantic. Candidates are not repeatedly scheduled for
this optional fact before an eligible manifest exists.

## Failure and rollback behavior

Invalid, future-known, incomplete, duplicate-symbol, guessed-license, symlink,
or unsupported-benchmark files fail before insertion. Existing evidence is
never rewritten. Operational rollback is therefore to stop importing new
manifests or disable the external-context worker; historical rows remain an
auditable record.
