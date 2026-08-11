from __future__ import annotations

from tradebot import accounts
from tradebot.entitlements import PLAN_LIMITS, DevBillingProvider, Plan, limits_for
from tradebot.telegram_bot import db


def test_every_plan_enum_value_has_limits_defined():
    for plan in Plan:
        assert plan in PLAN_LIMITS


def test_limits_for_unrecognized_plan_falls_back_to_beta_permissive_limits():
    assert limits_for("some-typo-plan") == PLAN_LIMITS[Plan.BETA]


def test_free_plan_is_more_restricted_than_pro():
    free = PLAN_LIMITS[Plan.FREE]
    pro = PLAN_LIMITS[Plan.PRO]
    assert free.watchlist_size is not None and pro.watchlist_size is None
    assert free.realtime_high_alerts is False and pro.realtime_high_alerts is True


def test_dev_billing_provider_get_and_set_plan_round_trip():
    conn = db.connect(":memory:")
    account = accounts.create_account(conn, email="alice@example.com")
    provider = DevBillingProvider(conn)

    assert provider.get_plan(account.id) == Plan.BETA

    provider.set_plan(account.id, Plan.PRO)
    assert provider.get_plan(account.id) == Plan.PRO
    assert accounts.get_account(conn, account.id).plan == "pro"


def test_dev_billing_provider_get_plan_unknown_account_raises():
    conn = db.connect(":memory:")
    provider = DevBillingProvider(conn)
    try:
        provider.get_plan("does-not-exist")
        assert False, "expected KeyError"
    except KeyError:
        pass
