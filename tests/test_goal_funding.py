from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Expense, SavingEvent
from app.services.goal_funding import calculate
from tests.test_finance import _budget, _decide, _goal, _impulse


def opening(on, balance=0, baseline=0, reserved=0):
    return dict(
        startsOn=on.isoformat(), openingBalance=balance, baselineSpent=baseline, reservedThisMonth=reserved
    )


@pytest.mark.parametrize("year,month,days", [(2025, 2, 28), (2024, 2, 29), (2026, 4, 30), (2026, 10, 31)])
def test_daily_accrual_conserves_monthly_capacity(year, month, days):
    start = date(year, month, 1)
    budgets = {start.strftime("%Y-%m"): 90001}
    initial = calculate(200000, opening(start), budgets, [], start)
    assert initial["saved"] == 0 and initial["daily"] == 90001 / days
    after = calculate(200000, opening(start), budgets, [], start + timedelta(days=days))
    assert after["saved"] == 90001
    assert after["eta"].month != month


def test_midmonth_registration_does_not_allocate_past_days_or_baseline_twice():
    start = date(2026, 11, 16)
    budgets = {"2026-11": 120000}
    expenses = [(date(2026, 11, 1), 30000)]
    f = opening(start, baseline=30000)
    first = calculate(100000, f, budgets, expenses, start)
    assert first["saved"] == 0 and first["daily"] == 6000
    assert first["eta"] == date(2026, 12, 3)
    tomorrow = calculate(100000, f, budgets, expenses, start + timedelta(days=1))
    assert tomorrow["saved"] == 6000
    assert tomorrow["saved"] + tomorrow["daily"] * 14 == 90000


def test_purchases_use_future_pool_then_reduce_saved_and_push_eta():
    start = date(2026, 11, 1)
    today = date(2026, 11, 16)
    budgets = {"2026-11": 90000}
    baseline = calculate(120000, opening(start), budgets, [], today)
    purchase = calculate(120000, opening(start), budgets, [(today, 60000)], today)
    assert baseline["saved"] == 45000
    assert purchase["saved"] == 30000 and purchase["daily"] is None
    assert purchase["eta"] > baseline["eta"]
    assert calculate(120000, opening(start), budgets, [], today, 60000) == purchase


def test_missing_or_nonpositive_budget_has_no_fabricated_eta():
    start = date(2026, 11, 1)
    for budgets in ({}, {"2026-11": 0}, {"2026-11": -10000}):
        result = calculate(100000, opening(start), budgets, [], start)
        assert result["saved"] == 0 and result["eta"] is None and result["daily"] is None


def test_same_month_replacement_reserves_previously_allocated_money():
    start = date(2026, 11, 16)
    result = calculate(
        100000, opening(start, balance=30000, reserved=45000), {"2026-11": 90000}, [], date(2026, 12, 1)
    )
    assert result["saved"] == 75000  # 15,000 stays with the old goal.


def test_calendar_achievement_is_once_only_and_resistance_is_separate(client, guest_headers, session, today):
    today.value = date(2026, 11, 16)
    _budget(client, guest_headers, month="2026-11", income=90000, food=0, living=0, savings=0)
    _goal(client, guest_headers, target=12000)
    impulse = _impulse(client, guest_headers, amount=90000)
    _decide(client, guest_headers, impulse, "RESISTED")
    assert client.get("/api/v1/goals/current", headers=guest_headers).json()["saved_amount"] == 0
    today.value += timedelta(days=2)
    for _ in range(2):
        shelf = client.get("/api/v1/goals/shelf", headers=guest_headers).json()
        assert shelf["count"] == 1 and shelf["items"][0]["saved_amount"] == 12000
    _decide(client, guest_headers, impulse, "CANCELLED")
    assert client.get("/api/v1/goals/shelf", headers=guest_headers).json() == shelf
    assert session.scalar(select(func.count(Expense.id))) == 0
    assert session.scalar(select(func.count(SavingEvent.id))) == 2
