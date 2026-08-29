"""Generates a static, self-contained public status page from real,
reproducible data:

- Uptime and the incident log come from tradebot.incidents.
- Missed alerts come from tradebot.metrics' validator_rejection
  counters — every HIGH-tier alert the data-integrity guard
  (tradebot.guard) suppressed before it could publish.
- Operational failure counters expose every recorded rejection,
  suppression, downgrade, and ``*_failed`` family. These are raw counter
  increments and may overlap; they are not presented as a deduplicated
  incident or missed-alert total.
- The alerts-published-vs-NO-TRADE counter comes from
  tradebot.telegram_bot.performance.track_record — the exact same query
  /performance uses, not a second copy that could drift.

Deliberately a static file, not a live server — consistent with this
project's preference for boring, dependency-free infrastructure
(tradebot.metrics, tradebot.telegram_bot.tokenbucket, etc. all made the
same call). Regenerate on a schedule (cron, or folded into runner.py's
existing daily cycle) and host the resulting file wherever's convenient
(GitHub Pages, S3, nginx) — this module only produces the file, it
never serves it.

Uptime is wall-clock, not RTH-minute-precise: 1 minus the fraction of
time since the first tracked incident spent inside an open incident
(planned halt or unplanned heartbeat staleness), of either kind. That's
a simpler, honest choice for a beta-stage bot rather than importing
exchange-calendar precision for a number this provisional — see
tradebot.incidents' module docstring for what counts as an incident.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradebot import incidents, metrics
from tradebot.telegram_bot import performance

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "status.html"

_INCIDENT_KIND_LABELS = {
    "halt": "Planned halt",
    "heartbeat_stale": "Unplanned outage (data feed / scanner)",
}


@dataclass(frozen=True)
class StatusPageData:
    generated_at: datetime
    uptime_pct: float | None  # None if no incident tracking has started yet — never fabricated as 100%
    tracked_since: datetime | None
    total_incident_seconds: float
    incidents: list[dict]
    missed_alerts_by_rule: dict[str, int]
    total_missed_alerts: int
    operational_failures_by_family: dict[str, int]
    total_alerts_published: int
    total_no_trade: int
    no_trade_tracked_count: int


def _total_incident_seconds(all_incidents: list[dict], now: datetime) -> float:
    total = 0.0
    for incident in all_incidents:
        start = datetime.fromisoformat(incident["started_at"])
        end = datetime.fromisoformat(incident["ended_at"]) if incident["ended_at"] else now
        total += (end - start).total_seconds()
    return total


def _missed_alerts_by_rule(all_metrics: dict) -> dict[str, int]:
    by_rule: dict[str, int] = {}
    for key, count in all_metrics.items():
        if not key.startswith("validator_rejection"):
            continue
        rule = key.split("rule=", 1)[1].rstrip("}") if "rule=" in key else "unknown"
        by_rule[rule] = by_rule.get(rule, 0) + count
    return by_rule


_OPERATIONAL_FAILURE_SUFFIXES = (
    "_error",
    "_failed",
    "_failure",
    "_rejection",
    "_suppression",
)
_OPERATIONAL_FAILURE_NAMES = {
    # Generic category counter written alongside more specific counters.
    "suppression",
    # A downgrade prevents the original HIGH route without suppressing the
    # detection entirely, so suffix matching alone would miss it.
    "event_window_downgrade",
}


def _metric_name(key: str) -> str:
    """Return the counter family without its ``{label=value}`` suffix."""
    return key.partition("{")[0]


def _operational_failures_by_family(all_metrics: dict) -> dict[str, int]:
    """Select raw operational failure/suppression counters for disclosure.

    Aggregate labelled keys by family. This preserves the public page's bounded
    size even for symbol-labelled counters while proving that no recorded
    failure family is invisible. ``validator_rejection`` is already rendered
    as the separately defined missed-alert table and is excluded here to avoid
    displaying the exact same counter twice.
    """
    selected: dict[str, int] = {}
    for key, count in all_metrics.items():
        name = _metric_name(key)
        if name == "validator_rejection":
            continue
        if name in _OPERATIONAL_FAILURE_NAMES or name.endswith(_OPERATIONAL_FAILURE_SUFFIXES):
            selected[name] = selected.get(name, 0) + count
    return selected


def collect_status_data(
    journal_conn,
    now: datetime | None = None,
    incidents_path: Path | None = None,
    metrics_path: Path | None = None,
) -> StatusPageData:
    now = now or datetime.now(timezone.utc)
    all_incidents = incidents.list_incidents(path=incidents_path)
    tracked_since = min((datetime.fromisoformat(i["started_at"]) for i in all_incidents), default=None)
    incident_seconds = _total_incident_seconds(all_incidents, now)

    uptime_pct = None
    if tracked_since is not None:
        total_tracked_seconds = (now - tracked_since).total_seconds()
        if total_tracked_seconds > 0:
            uptime_pct = 100.0 * (1 - incident_seconds / total_tracked_seconds)

    tr = performance.track_record(journal_conn, tier="high")
    all_metrics = metrics.read_all(path=metrics_path)
    missed_by_rule = _missed_alerts_by_rule(all_metrics)

    return StatusPageData(
        generated_at=now,
        uptime_pct=uptime_pct,
        tracked_since=tracked_since,
        total_incident_seconds=incident_seconds,
        incidents=sorted(all_incidents, key=lambda i: i["started_at"], reverse=True),
        missed_alerts_by_rule=missed_by_rule,
        total_missed_alerts=sum(missed_by_rule.values()),
        operational_failures_by_family=_operational_failures_by_family(all_metrics),
        total_alerts_published=tr.total_alerts if tr else 0,
        total_no_trade=tr.total_no_trade if tr else 0,
        no_trade_tracked_count=tr.no_trade_tracked_count if tr else 0,
    )


def _fmt_duration(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} hr"
    return f"{hours / 24:.1f} days"


def _render_incident_row(incident: dict) -> str:
    kind_label = html.escape(_INCIDENT_KIND_LABELS.get(incident["kind"], incident["kind"]))
    started = html.escape(incident["started_at"])
    if incident["ended_at"] is None:
        status = "<b>ONGOING</b>"
        duration = "—"
    else:
        status = "resolved"
        started_dt = datetime.fromisoformat(incident["started_at"])
        ended_dt = datetime.fromisoformat(incident["ended_at"])
        duration = _fmt_duration((ended_dt - started_dt).total_seconds())
    detail = html.escape(incident["detail"])
    return f"<tr><td>{started}</td><td>{kind_label}</td><td>{detail}</td><td>{duration}</td><td>{status}</td></tr>"


def render_status_page(data: StatusPageData) -> str:
    uptime_line = f"{data.uptime_pct:.2f}%" if data.uptime_pct is not None else "not enough history yet"
    tracked_since_line = html.escape(data.tracked_since.isoformat()) if data.tracked_since else "no incidents tracked yet"

    if data.incidents:
        incident_rows = "\n".join(_render_incident_row(i) for i in data.incidents)
        incident_table = (
            "<table><thead><tr><th>Started</th><th>Kind</th><th>Detail</th>"
            f"<th>Duration</th><th>Status</th></tr></thead><tbody>{incident_rows}</tbody></table>"
        )
    else:
        incident_table = "<p>No incidents recorded.</p>"

    if data.missed_alerts_by_rule:
        missed_rows = "\n".join(
            f"<tr><td>{html.escape(rule)}</td><td>{count}</td></tr>"
            for rule, count in sorted(data.missed_alerts_by_rule.items(), key=lambda kv: -kv[1])
        )
        missed_table = f"<table><thead><tr><th>Rule</th><th>Count</th></tr></thead><tbody>{missed_rows}</tbody></table>"
    else:
        missed_table = "<p>None recorded.</p>"

    if data.operational_failures_by_family:
        operational_rows = "\n".join(
            f"<tr><td>{html.escape(metric)}</td><td>{count}</td></tr>"
            for metric, count in sorted(
                data.operational_failures_by_family.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
        )
        operational_table = (
            "<table><thead><tr><th>Metric</th><th>Counter increments</th></tr>"
            f"</thead><tbody>{operational_rows}</tbody></table>"
        )
    else:
        operational_table = "<p>None recorded.</p>"

    no_trade_line = (
        f"{data.total_no_trade} of {data.no_trade_tracked_count} tracked HIGH alerts were NO TRADE "
        "(no tradable contract) — the system saying \"sit this one out.\""
        if data.no_trade_tracked_count
        else "not enough tracked history yet"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Watchtower status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; }}
  .stat {{ font-size: 1.6rem; font-weight: 600; }}
  .label {{ color: #666; font-size: 0.85rem; }}
  .beta {{ display: inline-block; background: #fff3cd; color: #664d03; padding: 0.1rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
  footer {{ margin-top: 3rem; font-size: 0.8rem; color: #888; }}
</style>
</head>
<body>
<h1>Watchtower <span class="beta">BETA</span></h1>
<p class="label">Generated {html.escape(data.generated_at.isoformat())}</p>

<h2>Uptime</h2>
<div class="stat">{uptime_line}</div>
<p class="label">Tracked since {tracked_since_line}. Wall-clock, includes planned halts — see the incident log below for the breakdown.</p>

<h2>Alerts published vs. NO TRADE</h2>
<p>{no_trade_line}</p>

<h2>Missed alerts (data-integrity guard rejections)</h2>
<p class="label">Every alert here was a real detection that never published because the numbers behind it didn't hold up — see tradebot.guard.</p>
{missed_table}

<h2>Operational failures, suppressions, and downgrades</h2>
<p class="label">Raw production counter increments. Counters can overlap when one event records both a specific failure and its suppression category, so they are not added into a fabricated incident total.</p>
{operational_table}

<h2>Incident log</h2>
{incident_table}

<footer>This is a discipline and journaling system built on a technical alert feed — not a proven trading edge. See /performance in the bot for the live, journal-derived track record.</footer>
</body>
</html>
"""


def generate_status_page(
    journal_conn,
    output_path: Path | None = None,
    now: datetime | None = None,
    incidents_path: Path | None = None,
    metrics_path: Path | None = None,
) -> Path:
    output_path = output_path or DEFAULT_OUTPUT_PATH
    data = collect_status_data(journal_conn, now=now, incidents_path=incidents_path, metrics_path=metrics_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_status_page(data))
    return output_path
