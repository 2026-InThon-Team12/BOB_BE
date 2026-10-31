from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import Goal, Impulse, SavingEvent
from app.services.finance import compare_spending, month_days


@pytest.mark.parametrize(
    ("month", "days"), [("2025-02", 28), ("2024-02", 29), ("2026-04", 30), ("2026-10", 31)]
)
def test_real_calendar_month_lengths(month, days):
    assert month_days(month) == days
    result = compare_spending(1, month, income=2, savings=1)
    # Rounding the displayed daily saving capacity first would give wrong delays.
    assert result.delay_days == days


@pytest.mark.parametrize("month", ["2026-2", "2026-00", "2026-13", "0000-01", "2026-10-01"])
def test_month_format_is_strict(month):
    with pytest.raises(ValueError):
        month_days(month)


def test_delay_rounds_up_exact_integer_ratio():
    result = compare_spending(
        30_000, "2026-10", income=1_300_000, food=300_000, living=400_000, savings=300_000
    )
    assert result.available_amount == 300_000
    assert result.delay_days == 4


def test_large_delay_remains_integer():
    assert (
        compare_spending(1_000_000_000_000, "2026-10", income=2, savings=1).delay_days == 31_000_000_000_000
    )


@pytest.mark.parametrize(("income", "food", "available"), [(100, 100, 0), (100, 200, -100)])
def test_no_available_budget_preserves_raw_amount(income, food, available):
    result = compare_spending(100, "2026-10", income=income, food=food)
    assert result.available_amount == available
    assert result.delay_days is None
    assert result.comparison_reason == "NO_AVAILABLE_BUDGET"


def test_missing_budget_is_not_zero_budget():
    result = compare_spending(100, "2026-10")
    assert result.available_amount is None
    assert result.delay_days is None
    assert result.comparison_reason == "BUDGET_REQUIRED"


def _goal(client, headers, target=100_000):
    response = client.post(
        "/api/v1/goals", headers=headers, json={"name": "미쿠 피규어", "target_amount": target}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _budget(
    client,
    headers,
    month="2026-10",
    income=1_000_000,
    food=300_000,
    living=400_000,
    transport=0,
    savings=0,
):
    response = client.put(
        f"/api/v1/budgets/{month}",
        headers=headers,
        json={"income": income, "food": food, "living": living, "transport": transport, "savings": savings},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _impulse(client, headers, key="impulse-request-001", amount=30_000):
    response = client.post(
        "/api/v1/impulses",
        headers={**headers, "Idempotency-Key": key},
        json={"name": "치킨", "amount": amount},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _decide(client, headers, impulse, decision):
    return client.put(
        f"/api/v1/impulses/{impulse['id']}/decision", headers=headers, json={"decision": decision}
    )


def test_impulse_snapshot_survives_budget_change_and_retries(client, guest_headers):
    _goal(client, guest_headers)
    _budget(client, guest_headers)
    original = _impulse(client, guest_headers)
    assert original["delay_days"] == 0
    assert original["is_estimate"] is True
    _budget(client, guest_headers, income=0)
    retried = _impulse(client, guest_headers)
    assert retried == original
    changed = client.post(
        "/api/v1/impulses",
        headers={**guest_headers, "Idempotency-Key": "impulse-request-001"},
        json={"name": "치킨", "amount": 31_000},
    )
    assert changed.status_code == 409


def test_virtual_credit_and_reversal_are_once_only_and_do_not_change_budget(
    client, guest_headers, db_session
):
    _goal(client, guest_headers, target=10_000)
    original_budget = _budget(client, guest_headers)
    impulse = _impulse(client, guest_headers, amount=30_000)
    for _ in range(2):
        assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 200
    goal = client.get("/api/v1/goals/current", headers=guest_headers).json()
    assert goal["saved_amount"] == 0
    assert goal["progress_percent"] == 0
    assert goal["achieved"] is False
    assert goal["is_virtual"] is True
    assert client.get("/api/v1/budgets/2026-10", headers=guest_headers).json() == original_budget
    for _ in range(2):
        assert _decide(client, guest_headers, impulse, "CANCELLED").status_code == 200
    goal = client.get("/api/v1/goals/current", headers=guest_headers).json()
    assert goal["saved_amount"] == 0
    assert goal["progress_percent"] == 0
    events = db_session.scalars(select(SavingEvent).where(SavingEvent.impulse_id == impulse["id"])).all()
    assert sorted((event.kind, event.amount) for event in events) == [
        ("CREDIT", 30_000),
        ("REVERSAL", -30_000),
    ]
    assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 409


def test_purchase_does_not_create_virtual_savings(client, guest_headers, db_session):
    _goal(client, guest_headers)
    impulse = _impulse(client, guest_headers)
    assert _decide(client, guest_headers, impulse, "PURCHASED").status_code == 200
    assert _decide(client, guest_headers, impulse, "PURCHASED").status_code == 200
    assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 409
    assert db_session.scalar(select(func.count()).select_from(SavingEvent)) == 0


def test_impulse_without_goal_can_compare_but_cannot_lose_savings(client, guest_headers):
    _budget(client, guest_headers)
    impulse = _impulse(client, guest_headers)
    assert impulse["goal_id"] is None
    assert impulse["delay_days"] == 1
    assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 200
    assert _goal(client, guest_headers)["saved_amount"] == 0
    assert _decide(client, guest_headers, impulse, "CANCELLED").status_code == 200
    assert client.get("/api/v1/goals/current", headers=guest_headers).json()["saved_amount"] == 0


def test_missing_budget_and_nonpositive_budget_are_exposed(client, guest_headers):
    missing = _impulse(client, guest_headers)
    assert missing["comparison_reason"] == "BUDGET_REQUIRED"
    assert missing["delay_days"] is None
    _budget(client, guest_headers, income=0, savings=0)
    negative = _impulse(client, guest_headers, key="impulse-request-002")
    assert negative["comparison_reason"] == "NO_AVAILABLE_BUDGET"
    assert negative["available_amount"] == -700000
    assert negative["delay_days"] is None


def test_active_goal_unique_and_editable(client, guest_headers):
    goal = _goal(client, guest_headers)
    duplicate = client.post(
        "/api/v1/goals", headers=guest_headers, json={"name": "키보드", "target_amount": 50_000}
    )
    assert duplicate.status_code == 409
    updated = client.patch("/api/v1/goals/current", headers=guest_headers, json={"target_amount": 200_000})
    assert updated.status_code == 200
    assert updated.json()["id"] == goal["id"]
    assert updated.json()["target_amount"] == 200_000
    assert client.patch("/api/v1/goals/current", headers=guest_headers, json={}).status_code == 422
    assert (
        client.patch("/api/v1/goals/current", headers=guest_headers, json={"name": None}).status_code == 422
    )


def test_finance_records_are_scoped_to_guest(client, guest_headers):
    _goal(client, guest_headers)
    _budget(client, guest_headers)
    impulse = _impulse(client, guest_headers)
    other_token = client.post("/api/v1/guest-sessions").json()["access_token"]
    other = {"Authorization": "Bearer " + other_token}
    assert client.get("/api/v1/goals/current", headers=other).status_code == 404
    assert client.get("/api/v1/budgets/2026-10", headers=other).status_code == 404
    assert _decide(client, other, impulse, "RESISTED").status_code == 404
    # Keys are scoped per owner, so another guest can use the same key safely.
    other_impulse = _impulse(client, other)
    assert other_impulse["id"] != impulse["id"]


@pytest.mark.parametrize("bad_amount", [True, 1.5, "1000", 0, -1, 1_000_000_000_001])
def test_money_is_strict_positive_integer(client, guest_headers, bad_amount):
    response = client.post(
        "/api/v1/impulses",
        headers={**guest_headers, "Idempotency-Key": "bad-money-request"},
        json={"name": "치킨", "amount": bad_amount},
    )
    assert response.status_code == 422


def test_budget_month_validation_and_idempotent_put(client, guest_headers):
    first = _budget(client, guest_headers, month="2024-02")
    second = _budget(client, guest_headers, month="2024-02")
    assert first == second
    assert first["month_days"] == 29
    body = {"income": 0, "food": 0, "living": 0, "transport": 0}
    for month in ("2026-13", "0000-01"):
        assert client.put(f"/api/v1/budgets/{month}", headers=guest_headers, json=body).status_code == 422


def test_large_delay_can_be_persisted_without_database_integer_overflow(client, guest_headers):
    _budget(client, guest_headers, income=1, food=0, living=0, transport=0, savings=0)
    impulse = _impulse(client, guest_headers, amount=1_000_000_000_000)
    assert impulse["delay_days"] == 1_000_000_000_000


def test_impulse_registration_requires_idempotency_key(client, guest_headers):
    assert (
        client.post(
            "/api/v1/impulses", headers=guest_headers, json={"name": "치킨", "amount": 30_000}
        ).status_code
        == 422
    )


def test_postgresql_concurrent_goal_creation_has_one_winner(client, guest_headers, db_session):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("row-lock concurrency requires PostgreSQL")

    def create(name):
        return client.post(
            "/api/v1/goals", headers=guest_headers, json={"name": name, "target_amount": 100_000}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, ["피규어", "키보드"]))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert db_session.scalar(select(func.count()).select_from(Goal)) == 1


def test_postgresql_concurrent_impulse_retry_creates_one_record(client, guest_headers, db_session):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("row-lock concurrency requires PostgreSQL")
    _goal(client, guest_headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _impulse(client, guest_headers), range(2)))
    assert responses[0]["id"] == responses[1]["id"]
    assert db_session.scalar(select(func.count()).select_from(Impulse)) == 1


def test_postgresql_concurrent_resist_credits_once(client, guest_headers, db_session):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("row-lock concurrency requires PostgreSQL")
    _goal(client, guest_headers)
    impulse = _impulse(client, guest_headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _decide(client, guest_headers, impulse, "RESISTED"), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert client.get("/api/v1/goals/current", headers=guest_headers).json()["saved_amount"] == 0
    assert (
        db_session.scalar(
            select(func.count()).select_from(SavingEvent).where(SavingEvent.impulse_id == impulse["id"])
        )
        == 1
    )


def _accrue(client, headers, today, amount):
    """Fund one completed calendar day, independently of resistance events."""
    days = month_days(today.value.strftime('%Y-%m')) - today.value.day + 1
    _budget(client, headers, month=today.value.strftime('%Y-%m'), income=amount * days,
            food=0, living=0, transport=0, savings=0)
    today.value += timedelta(days=1)
    result = client.get('/api/v1/goals/current', headers=headers)
    assert result.status_code == 200
    return result.json()
