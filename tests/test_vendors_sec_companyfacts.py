"""SEC point-in-time facts are acceptance-bounded and fixture-driven."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradebot.vendors import sec_companyfacts as sec


CUTOFF = datetime(2026, 8, 27, 20, 30, tzinfo=timezone.utc)
OLD_ACCEPTED = CUTOFF - timedelta(days=30)
NEAR_ACCEPTED = CUTOFF - timedelta(minutes=5)
FUTURE_ACCEPTED = CUTOFF + timedelta(minutes=1)


def _submissions_payload():
    rows = [
        ("0000000001-26-000001", "10-Q", "2026-07-31", "2026-06-30", OLD_ACCEPTED, "q.htm"),
        ("0000000001-26-000002", "8-K", "2026-08-27", "2026-08-27", NEAR_ACCEPTED, "x.htm"),
        ("0000000001-26-000003", "10-Q", "2026-08-27", "2026-08-27", FUTURE_ACCEPTED, "f.htm"),
    ]
    return {"filings": {"recent": {
        "accessionNumber": [row[0] for row in rows],
        "form": [row[1] for row in rows],
        "filingDate": [row[2] for row in rows],
        "reportDate": [row[3] for row in rows],
        "acceptanceDateTime": [row[4].isoformat().replace("+00:00", "Z") for row in rows],
        "primaryDocument": [row[5] for row in rows],
    }}}


def _fact(val, accn, *, end="2026-06-30", start=None, form="10-Q"):
    result = {
        "end": end, "val": val, "accn": accn, "form": form,
        "filed": "2026-07-31", "fy": 2026, "fp": "Q2", "frame": "CY2026Q2I",
    }
    if start is not None:
        result["start"] = start
        result["frame"] = "CY2026Q2"
    return result


def _companyfacts_payload():
    old = "0000000001-26-000001"
    near = "0000000001-26-000002"
    future = "0000000001-26-000003"
    return {"facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                _fact(100, old), _fact(999, future, end="2026-08-27"),
            ]}},
            "EntityPublicFloat": {"units": {"USD": [_fact(5000, old)]}},
        },
        "us-gaap": {
            "Assets": {"units": {"USD": [_fact(1000, old), _fact(5000, near)]}},
            "Liabilities": {"units": {"USD": [_fact(400, old)]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                _fact(300, old, start="2026-04-01"),
                _fact(900, old, start="2026-01-01"),
            ]}},
            "NetIncomeLoss": {"units": {"USD": [
                _fact(-20, old, start="2026-04-01"),
            ]}},
        },
    }}


def test_recent_submission_columns_are_strict_and_sorted():
    rows = sec.parse_recent_submissions(_submissions_payload())
    assert len(rows) == 3
    assert rows[0].accepted_at_utc == OLD_ACCEPTED
    broken = _submissions_payload()
    broken["filings"]["recent"]["form"].pop()
    with pytest.raises(sec.SecCompanyFactsError, match="inconsistent"):
        sec.parse_recent_submissions(broken)


def test_facts_join_to_acceptance_and_apply_conservative_safety_lag():
    submissions = sec.parse_recent_submissions(_submissions_payload())
    facts = sec.select_reported_facts(
        _companyfacts_payload(), submissions, cutoff=CUTOFF,
    )
    by_name = {fact.name: fact for fact in facts}
    assert by_name["common_shares_outstanding"].value == 100
    assert by_name["assets"].value == 1000
    assert by_name["revenue"].value == 300
    assert by_name["net_income"].value == -20
    assert all(fact.accepted_at_utc <= CUTOFF - sec.DISSEMINATION_SAFETY_LAG for fact in facts)
    assert "stockholders_equity" not in by_name


class _Response:
    def __init__(self, *, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_filing_header_uses_eastern_acceptance_and_rejects_mismatch(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Perch test test@example.com")
    submission = sec.parse_recent_submissions(_submissions_payload())[0]
    # 16:30 ET is 20:30 UTC during August.
    accepted = datetime(2026, 7, 28, 16, 30, tzinfo=sec.ET).astimezone(timezone.utc)
    submission = sec.Submission(
        submission.accession_number, submission.form, submission.filing_date,
        submission.report_date, accepted, submission.primary_document,
    )
    text = "<ACCEPTANCE-DATETIME>20260728163000\nSTANDARD INDUSTRIAL CLASSIFICATION: ELECTRONIC COMPUTERS [3571]\n"
    assert sec.fetch_filing_industry(
        "0000000001", submission, session=_Session([_Response(text=text)]),
    ) == ("3571", "ELECTRONIC COMPUTERS")
    mismatch = text.replace("163000", "163001")
    with pytest.raises(sec.SecCompanyFactsError, match="did not match"):
        sec.fetch_filing_industry(
            "0000000001", submission, session=_Session([_Response(text=mismatch)]),
        )


def test_point_in_time_snapshot_has_exact_provenance_and_no_future_facts(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Perch test test@example.com")
    submissions = _submissions_payload()
    facts = _companyfacts_payload()
    classification = sec.parse_recent_submissions(submissions)[0]
    accepted_et = classification.accepted_at_utc.astimezone(sec.ET).strftime("%Y%m%d%H%M%S")
    header = (
        f"<ACCEPTANCE-DATETIME>{accepted_et}\n"
        "STANDARD INDUSTRIAL CLASSIFICATION: SERVICES-PREPACKAGED SOFTWARE [7372]\n"
    )
    client = _Session([
        _Response(payload=submissions), _Response(payload=facts), _Response(text=header),
    ])
    snapshot = sec.fetch_point_in_time_snapshot(
        "ABC", CUTOFF, cik="0000000001", session=client,
    )
    assert snapshot.cik == "0000000001"
    assert snapshot.eligible_cutoff_utc == CUTOFF - timedelta(minutes=15)
    assert snapshot.classification_accession == classification.accession_number
    assert snapshot.sic_code == "7372"
    assert snapshot.errors == ()
    assert all(fact.accession_number != "0000000001-26-000003" for fact in snapshot.facts)
    assert len(client.calls) == 3


def test_missing_user_agent_fails_before_network(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(sec.SecCompanyFactsError, match="not configured"):
        sec.fetch_submissions("0000000001", session=_Session([]))
