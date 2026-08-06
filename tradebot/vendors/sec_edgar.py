"""SEC EDGAR adapter — the only file allowed to talk to EDGAR directly,
same rule as tradebot/vendors/alpaca.py. Free, no API key, no rate limit
beyond SEC's fair-access policy (which requires a real identifying
User-Agent — see SEC_EDGAR_USER_AGENT below; requests without one get
blocked).

This is filing METADATA only (form type, filing date, accession number)
— never the filing text itself, never a sentiment score, never a "what
does this mean" judgment. That classification (which form types suppress
vs. which are context-only) lives in tradebot.events, not here. This
module's only job is: what got filed, for which company, on what date.

EDGAR's browse feed gives a filing DATE, not a timestamp — there's no
free intraday filing-time feed, so every event window built from this
data necessarily covers the whole session, not a tighter intraday
window. That's a real limitation of the source, not a shortcut taken
here.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CIK_CACHE_PATH = REPO_ROOT / "data" / "cache" / "sec_cik_map.json"
CIK_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # tickers/CIKs change rarely — weekly refresh is plenty

# SEC blocks requests without a real, identifying User-Agent — see
# https://www.sec.gov/os/webmaster-faq#developers. Set this to something
# that actually identifies your deployment and a contact address; the
# default below is a placeholder, not a real contact, and SEC may
# rate-limit or block it under sustained use.
DEFAULT_USER_AGENT = "watchtower-market-scanner (set SEC_EDGAR_USER_AGENT to a real contact)"

FORM_TYPES = ("8-K", "SC 13D", "SC 13G", "4")


def _headers() -> dict:
    return {"User-Agent": os.environ.get("SEC_EDGAR_USER_AGENT", DEFAULT_USER_AGENT)}


def _with_backoff(fn, max_retries: int = 3, base_delay: float = 2.0):
    for attempt in range(max_retries):
        try:
            return fn()
        except requests.exceptions.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (2**attempt))


def _read_cache() -> dict | None:
    if not CIK_CACHE_PATH.exists():
        return None
    try:
        return json.loads(CIK_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def fetch_cik_map(force_refresh: bool = False) -> dict:
    """ticker -> zero-padded 10-digit CIK, from SEC's own public
    ticker/CIK mapping. Cached locally (tickers/CIKs change rarely) so
    every symbol lookup doesn't refetch a 10k+-entry JSON file.

    Degrades the same way fetch_filings() does: a fresh-enough cache is
    used as-is; a fetch failure (network, a non-2xx from SEC — including
    the 403 SEC returns for a non-identifying User-Agent, see
    SEC_EDGAR_USER_AGENT — or a malformed response) falls back to
    whatever cache exists, even if it's stale, rather than raising. Only
    returns {} if there is truly no usable data anywhere: a real fetch
    failure should never crash the caller, but it also shouldn't silently
    hand back a plausible-looking empty result when a stale-but-real
    mapping was available instead."""
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            age = time.time() - CIK_CACHE_PATH.stat().st_mtime
            if age < CIK_CACHE_MAX_AGE_SECONDS:
                return cached

    try:
        resp = _with_backoff(lambda: requests.get(
            "https://www.sec.gov/files/company_tickers.json", headers=_headers(), timeout=15
        ))
        resp.raise_for_status()
        raw = resp.json()
        mapping = {entry["ticker"]: f"{entry['cik_str']:010d}" for entry in raw.values()}
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return _read_cache() or {}

    CIK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CIK_CACHE_PATH.write_text(json.dumps(mapping))
    return mapping


@dataclass(frozen=True)
class Filing:
    form_type: str
    filing_date: date
    accession_number: str
    items_desc: str | None


def fetch_filings(cik: str, form_type: str, count: int = 20) -> list[Filing]:
    """Real filings for one CIK and one EDGAR form type — call once per
    type you care about (EDGAR's `type` filter is a single form code,
    not a list). Returns [] on any failure — a missed filing means a
    suppress window doesn't get created, which is the same fail-safe
    direction as everything else in this project: never guess, never
    crash the caller over one bad HTTP response."""
    params = {
        "action": "getcompany", "CIK": cik, "type": form_type,
        "dateb": "", "owner": "include", "count": count, "output": "atom",
    }
    try:
        resp = _with_backoff(lambda: requests.get(
            "https://www.sec.gov/cgi-bin/browse-edgar", params=params, headers=_headers(), timeout=20
        ))
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except (requests.exceptions.RequestException, ET.ParseError):
        return []

    filings = []
    for entry in root.findall("a:entry", ATOM_NS):
        content = entry.find("a:content", ATOM_NS)
        if content is None:
            continue
        filing_type_el = content.find("a:filing-type", ATOM_NS)
        filing_date_el = content.find("a:filing-date", ATOM_NS)
        accession_el = content.find("a:accession-number", ATOM_NS)
        items_el = content.find("a:items-desc", ATOM_NS)
        if filing_type_el is None or filing_date_el is None or accession_el is None:
            continue
        try:
            filing_date = date.fromisoformat(filing_date_el.text)
        except (TypeError, ValueError):
            continue
        filings.append(Filing(
            form_type=filing_type_el.text, filing_date=filing_date,
            accession_number=accession_el.text, items_desc=items_el.text if items_el is not None else None,
        ))
    return filings


def fetch_all_filings(symbol: str, cik_map: dict, count_per_type: int = 20) -> list[Filing]:
    """All tracked form types for one symbol, or [] if the symbol has no
    known CIK (an unlisted or delisted ticker, or a stale cache — never
    guessed at)."""
    cik = cik_map.get(symbol)
    if cik is None:
        return []
    filings = []
    for form_type in FORM_TYPES:
        filings.extend(fetch_filings(cik, form_type, count=count_per_type))
    return filings
