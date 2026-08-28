"""Acceptance-bounded SEC industry and XBRL facts for historical replay.

The current Company Facts response is fetched after the event, but a fact is
eligible only when its accession joins to a submission accepted sufficiently
before the candidate cutoff.  The lag is deliberately conservative because
the SEC documents typical API publication delays, not a guaranteed first-
availability timestamp.  No missing concept is imputed and SEC SIC is never
called GICS or mapped to an ETF.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

from tradebot.vendors.sec_edgar import fetch_cik_map


BASE_URL = "https://data.sec.gov"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
DISSEMINATION_SAFETY_LAG = timedelta(minutes=15)
ISSUER_FORMS = frozenset({"8-K", "10-K", "10-Q", "20-F", "40-F", "6-K"})
ET = ZoneInfo("America/New_York")


class SecCompanyFactsError(RuntimeError):
    """Credential-free provider error without response bodies or URLs."""


@dataclass(frozen=True)
class Submission:
    accession_number: str
    form: str
    filing_date: date
    report_date: date | None
    accepted_at_utc: datetime
    primary_document: str | None


@dataclass(frozen=True)
class ReportedFact:
    name: str
    taxonomy: str
    concept: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    accession_number: str
    form: str
    filed: date
    accepted_at_utc: datetime
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None


@dataclass(frozen=True)
class PointInTimeSnapshot:
    symbol: str
    cik: str
    candidate_cutoff_utc: datetime
    eligible_cutoff_utc: datetime
    classification_accession: str | None
    classification_accepted_at_utc: datetime | None
    sic_code: str | None
    sic_description: str | None
    facts: tuple[ReportedFact, ...]
    recent_submission_count: int
    eligible_submission_count: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _FactSpec:
    name: str
    taxonomy: str
    concept: str
    unit: str
    duration: bool
    nonnegative: bool


FACT_SPECS = (
    _FactSpec("common_shares_outstanding", "dei", "EntityCommonStockSharesOutstanding", "shares", False, True),
    _FactSpec("public_float_value", "dei", "EntityPublicFloat", "USD", False, True),
    _FactSpec("assets", "us-gaap", "Assets", "USD", False, True),
    _FactSpec("liabilities", "us-gaap", "Liabilities", "USD", False, True),
    _FactSpec("stockholders_equity", "us-gaap", "StockholdersEquity", "USD", False, False),
    _FactSpec("cash_and_equivalents", "us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", False, True),
    _FactSpec("revenue", "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", True, False),
    _FactSpec("net_income", "us-gaap", "NetIncomeLoss", "USD", True, False),
)


def _headers() -> dict[str, str]:
    value = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not value:
        raise SecCompanyFactsError("SEC_EDGAR_USER_AGENT is not configured")
    return {"User-Agent": value, "Accept-Encoding": "gzip, deflate"}


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _request(
    url: str, *, as_json: bool, session: requests.Session | None = None,
) -> Any:
    client = session or requests.Session()
    try:
        response = client.get(
            url, headers=_headers(), timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        return response.json() if as_json else response.text
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status is not None else ""
        raise SecCompanyFactsError(f"SEC request failed{suffix}") from exc
    except ValueError as exc:
        raise SecCompanyFactsError("SEC returned invalid JSON") from exc


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("acceptanceDateTime was not a string")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), "acceptanceDateTime")


def _optional_date(value: object) -> date | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ValueError("SEC date was not a string")
    return date.fromisoformat(value)


def parse_recent_submissions(payload: Mapping[str, object]) -> tuple[Submission, ...]:
    try:
        recent = payload["filings"]["recent"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise SecCompanyFactsError("SEC submissions payload was missing recent filings") from exc
    if not isinstance(recent, Mapping):
        raise SecCompanyFactsError("SEC recent filings were not an object")
    fields = (
        "accessionNumber", "form", "filingDate", "reportDate",
        "acceptanceDateTime", "primaryDocument",
    )
    values = [recent.get(field) for field in fields]
    if any(not isinstance(value, list) for value in values):
        raise SecCompanyFactsError("SEC recent filing columns were invalid")
    lengths = {len(value) for value in values if isinstance(value, list)}
    if len(lengths) != 1:
        raise SecCompanyFactsError("SEC recent filing columns had inconsistent lengths")
    rows = []
    seen = set()
    try:
        for raw in zip(*values):
            accession = str(raw[0])
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or accession in seen:
                raise ValueError("invalid or duplicate accession number")
            seen.add(accession)
            rows.append(Submission(
                accession, str(raw[1]), date.fromisoformat(str(raw[2])),
                _optional_date(raw[3]), _parse_datetime(raw[4]),
                str(raw[5]) if raw[5] not in {None, ""} else None,
            ))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SecCompanyFactsError("SEC recent filing row was invalid") from exc
    return tuple(sorted(rows, key=lambda value: (value.accepted_at_utc, value.accession_number)))


def fetch_submissions(
    cik: str, *, session: requests.Session | None = None,
) -> tuple[Submission, ...]:
    if not re.fullmatch(r"\d{10}", cik):
        raise ValueError("cik must contain exactly ten digits")
    payload = _request(f"{BASE_URL}/submissions/CIK{cik}.json", as_json=True, session=session)
    if not isinstance(payload, Mapping):
        raise SecCompanyFactsError("SEC submissions response was not an object")
    return parse_recent_submissions(payload)


def _base_form(value: str) -> str:
    return value[:-2] if value.endswith("/A") else value


def _finite(value: object, *, nonnegative: bool) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or (nonnegative and parsed < 0):
        raise ValueError("XBRL fact value was invalid")
    return parsed


def _select_fact(
    payload: Mapping[str, object], spec: _FactSpec,
    acceptance_by_accession: Mapping[str, datetime],
) -> ReportedFact | None:
    try:
        concept = payload["facts"][spec.taxonomy][spec.concept]  # type: ignore[index]
        entries = concept["units"][spec.unit]
    except (KeyError, TypeError):
        return None
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise SecCompanyFactsError(f"SEC XBRL unit rows were invalid for {spec.concept}")
    eligible = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise SecCompanyFactsError(f"SEC XBRL row was invalid for {spec.concept}")
        accession = entry.get("accn")
        accepted = acceptance_by_accession.get(str(accession))
        if accepted is None:
            continue
        try:
            period_end = date.fromisoformat(str(entry["end"]))
            period_start = _optional_date(entry.get("start"))
            filed = date.fromisoformat(str(entry["filed"]))
            form = str(entry["form"])
            value = _finite(entry["val"], nonnegative=spec.nonnegative)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SecCompanyFactsError(f"SEC XBRL row was invalid for {spec.concept}") from exc
        if _base_form(form) not in ISSUER_FORMS:
            continue
        if spec.duration != (period_start is not None):
            continue
        duration_days = (period_end - period_start).days if period_start else 0
        if period_start and duration_days <= 0:
            continue
        eligible.append((
            period_end, accepted, -duration_days,
            ReportedFact(
                spec.name, spec.taxonomy, spec.concept, spec.unit, value,
                period_start, period_end, str(accession), form, filed, accepted,
                int(entry["fy"]) if isinstance(entry.get("fy"), int) else None,
                str(entry["fp"]) if entry.get("fp") is not None else None,
                str(entry["frame"]) if entry.get("frame") is not None else None,
            ),
        ))
    return max(eligible, key=lambda value: value[:3])[3] if eligible else None


def select_reported_facts(
    payload: Mapping[str, object], submissions: Sequence[Submission], *, cutoff: datetime,
) -> tuple[ReportedFact, ...]:
    eligible_cutoff = _utc(cutoff, "cutoff") - DISSEMINATION_SAFETY_LAG
    acceptance = {
        row.accession_number: row.accepted_at_utc
        for row in submissions if row.accepted_at_utc <= eligible_cutoff
    }
    return tuple(
        fact for spec in FACT_SPECS
        if (fact := _select_fact(payload, spec, acceptance)) is not None
    )


def fetch_companyfacts(
    cik: str, *, session: requests.Session | None = None,
) -> Mapping[str, object]:
    if not re.fullmatch(r"\d{10}", cik):
        raise ValueError("cik must contain exactly ten digits")
    payload = _request(
        f"{BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json",
        as_json=True, session=session,
    )
    if not isinstance(payload, Mapping):
        raise SecCompanyFactsError("SEC companyfacts response was not an object")
    return payload


def fetch_filing_industry(
    cik: str, submission: Submission, *, session: requests.Session | None = None,
) -> tuple[str, str]:
    compact = submission.accession_number.replace("-", "")
    text = _request(
        f"{ARCHIVES_URL}/{int(cik)}/{compact}/{submission.accession_number}-index-headers.html",
        as_json=False, session=session,
    )
    if not isinstance(text, str):
        raise SecCompanyFactsError("SEC filing header response was not text")
    accepted_match = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", text)
    sic_match = re.search(
        r"STANDARD INDUSTRIAL CLASSIFICATION:\s*([^\r\n\[]+?)\s*\[(\d{4})\]",
        text,
    )
    if accepted_match is None or sic_match is None:
        raise SecCompanyFactsError("SEC filing header lacked acceptance or SIC")
    accepted = datetime.strptime(accepted_match.group(1), "%Y%m%d%H%M%S").replace(
        tzinfo=ET,
    ).astimezone(timezone.utc)
    if accepted != submission.accepted_at_utc.replace(microsecond=0):
        raise SecCompanyFactsError("SEC filing header acceptance did not match submissions")
    return sic_match.group(2), " ".join(sic_match.group(1).split())


def fetch_point_in_time_snapshot(
    symbol: str,
    cutoff: datetime,
    *,
    cik: str | None = None,
    session: requests.Session | None = None,
) -> PointInTimeSnapshot:
    canonical = symbol.strip().upper()
    if not canonical or canonical != symbol:
        raise ValueError("symbol must be canonical uppercase")
    cutoff_utc = _utc(cutoff, "cutoff")
    if cik is None:
        cik = fetch_cik_map().get(canonical)
    if cik is None:
        raise SecCompanyFactsError("SEC CIK mapping was unavailable for symbol")
    submissions = fetch_submissions(cik, session=session)
    eligible_cutoff = cutoff_utc - DISSEMINATION_SAFETY_LAG
    eligible = tuple(row for row in submissions if row.accepted_at_utc <= eligible_cutoff)
    payload = fetch_companyfacts(cik, session=session)
    facts = select_reported_facts(payload, submissions, cutoff=cutoff_utc)
    issuer = [row for row in eligible if _base_form(row.form) in ISSUER_FORMS]
    classification = max(issuer, key=lambda row: row.accepted_at_utc) if issuer else None
    errors = []
    sic_code = sic_description = None
    if classification is not None:
        try:
            sic_code, sic_description = fetch_filing_industry(
                cik, classification, session=session,
            )
        except SecCompanyFactsError as exc:
            errors.append(type(exc).__name__)
    return PointInTimeSnapshot(
        canonical, cik, cutoff_utc, eligible_cutoff,
        classification.accession_number if classification else None,
        classification.accepted_at_utc if classification else None,
        sic_code, sic_description, facts, len(submissions), len(eligible), tuple(errors),
    )
