"""Plan definitions and the billing seam.

`Plan` and `PLAN_LIMITS` are the actual gating rules for once gating is
turned on (see tradebot.telegram_bot.access — nothing routes through
PLAN_LIMITS yet; today access.can_access still returns True for
everything, per the plan's own instruction that FREE must stay
genuinely useful until the product has proven its value). Building the
real model now, ungated, means turning gating on later is a one-line
change in access.py, not a redesign.

BillingProvider is the interface a future StripeBillingProvider
implements without any caller changing: DevBillingProvider today reads/
writes `accounts.plan` directly (no payment, no webhook) — same shape,
zero network calls.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum


class Plan(str, Enum):
    BETA = "beta"
    FREE = "free"
    PRO = "pro"
    FOUNDING_MEMBER = "founding_member"


@dataclass(frozen=True)
class PlanLimits:
    watchlist_size: int | None  # None = unlimited
    realtime_high_alerts: bool  # False = delayed/reduced HIGH delivery
    analytics_access: bool
    sensitivity_customization: bool


# BETA mirrors today's actual behavior (everyone gets full access) —
# see access.py's docstring: "Everything is free during beta."
PLAN_LIMITS: dict[Plan, PlanLimits] = {
    Plan.BETA: PlanLimits(
        watchlist_size=None, realtime_high_alerts=True, analytics_access=True, sensitivity_customization=True,
    ),
    Plan.FREE: PlanLimits(
        watchlist_size=5, realtime_high_alerts=False, analytics_access=False, sensitivity_customization=False,
    ),
    Plan.PRO: PlanLimits(
        watchlist_size=None, realtime_high_alerts=True, analytics_access=True, sensitivity_customization=True,
    ),
    Plan.FOUNDING_MEMBER: PlanLimits(
        watchlist_size=None, realtime_high_alerts=True, analytics_access=True, sensitivity_customization=True,
    ),
}


def limits_for(plan: str) -> PlanLimits:
    """Falls back to BETA's (most permissive) limits for an unrecognized
    plan string rather than raising — a typo in a stored plan value must
    never be the thing that locks someone out of the product."""
    try:
        return PLAN_LIMITS[Plan(plan)]
    except ValueError:
        return PLAN_LIMITS[Plan.BETA]


class BillingProvider:
    """Interface only — see DevBillingProvider for the one implementation
    that exists today."""

    def get_plan(self, account_id: str) -> Plan:
        raise NotImplementedError

    def set_plan(self, account_id: str, plan: Plan) -> None:
        raise NotImplementedError


class DevBillingProvider(BillingProvider):
    """No payment processor. Reads/writes accounts.plan directly — this
    is what makes /tiers show a real value and what a founding-member
    grant or an admin plan change actually persists, before Stripe (or
    anything else) exists."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_plan(self, account_id: str) -> Plan:
        row = self._conn.execute("SELECT plan FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such account: {account_id!r}")
        return Plan(row[0])

    def set_plan(self, account_id: str, plan: Plan) -> None:
        self._conn.execute("UPDATE accounts SET plan = ? WHERE id = ?", (plan.value, account_id))
        self._conn.commit()
