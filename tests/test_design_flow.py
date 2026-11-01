from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Expense, SavingEvent, SimulationExtension
from app.services.finance import compare_spending
from tests.test_core import create_item, answer
from tests.test_finance import _accrue, _goal, _budget, _impulse, _decide

P = "/api/v1"


def test_multiple_reasons_and_one_question_replay(client, guest_headers, today):
    body = {
        "items": [
            {
                "name": "코트",
                "price": 189000,
                "category": "CLOTHING",
                "purchase_reasons": ["SOCIAL", "ADVERTISEMENT"],
                "question_mode": "LEGACY",
            }
        ]
    }
    headers = {**guest_headers, "Idempotency-Key": "multi-reasons-1"}
    item = client.post(P + "/items/batch", headers=headers, json=body).json()["items"][0]
    assert item["purchase_reasons"] == ["ADVERTISEMENT", "SOCIAL"]
    body["items"][0]["purchase_reasons"].reverse()
    assert client.post(P + "/items/batch", headers=headers, json=body).json()["items"][0]["id"] == item["id"]
    body["items"][0]["purchase_reasons"] = ["SOCIAL", "SOCIAL"]
    assert client.post(P + "/items/batch", headers=headers, json=body).status_code == 422
    today.value += timedelta(days=1)
    card = client.get(P + "/today", headers=guest_headers).json()["questions"][0]
    assert card["question"] and card["current_day"] == 1
    url = f"{P}/simulations/{card['simulation_id']}/answers/{today.value}"
    assert client.put(url, headers=guest_headers, json={"usage_answer": "YES"}).status_code == 200
    done = client.get(P + "/today", headers=guest_headers).json()
    assert done["questions"] == [] and done["summary"]["counts"]["YES"] == 1
    replay = client.get(P + "/today?include_answered=true", headers=guest_headers).json()
    assert replay["questions"][0]["answer"] == "YES"
    assert client.put(url, headers=guest_headers, json={"usage_answer": "NO"}).status_code == 200
    assert client.get(P + "/today", headers=guest_headers).json()["summary"]["counts"]["NO"] == 1


def test_delayed_hold_extends_seven_observation_days_and_retries_once(client, guest_headers, today, session):
    item = create_item(client, guest_headers)
    sid = item["simulation"]["id"]
    today.value = date(2026, 11, 1)
    answer(client, guest_headers, sid, today.value)
    today.value = date(2026, 11, 10)
    headers = {**guest_headers, "Idempotency-Key": "hold-extension-1"}
    url = f"{P}/items/{item['id']}/decision"
    if session.bind.dialect.name == "postgresql":
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda _: client.put(url, headers=headers, json={"decision": "HOLD"}), range(2))
            )
    else:
        responses = [client.put(url, headers=headers, json={"decision": "HOLD"}) for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    assert session.scalar(select(func.count(SimulationExtension.id))) == 1
    held = responses[0].json()["simulation"]
    assert held["duration_days"] == 10 and held["end_date_exclusive"] == "2026-11-18"
    assert held["status"] == "ON_HOLD"
    assert answer(client, guest_headers, sid, today.value).status_code == 409
    today.value += timedelta(days=1)
    assert answer(client, guest_headers, sid, today.value).status_code == 200
    result = client.get(f"{P}/simulations/{sid}/result", headers=guest_headers).json()
    assert result["observed_days"] == 4 and result["unanswered_days"] == 2
    assert len(result["answer_timeline"]) == 4
    assert result["pending_days"] == 6


def test_bye_and_impulse_share_goal_ledger_and_next_goal(client, guest_headers, today, session):
    item = create_item(client, guest_headers)
    today.value = date(2026, 11, 4)
    url = f"{P}/items/{item['id']}/decision"
    for _ in range(2):
        assert client.put(url, headers=guest_headers, json={"decision": "GIVE_UP"}).status_code == 200
    assert session.scalar(select(func.count(SavingEvent.id))) == 1
    assert _goal(client, guest_headers, target=35000)["saved_amount"] == 0
    impulse = _impulse(client, guest_headers, amount=10000)
    for _ in range(2):
        assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 200
    goal = client.get(P + "/goals/current", headers=guest_headers).json()
    assert goal["saved_amount"] == 0 and goal["progress_percent"] == 0
    assert goal["resisted_count"] == 2 and goal["most_resisted_item"]
    _accrue(client, guest_headers, today, 35000)
    pending = _impulse(client, guest_headers, key="pending-new-goal")
    next_goal = client.post(
        P + "/goals/current/next", headers=guest_headers, json={"name": "다음 목표", "target_amount": 100000}
    )
    assert next_goal.status_code == 201 and next_goal.json()["saved_amount"] == 0
    # A later decision belongs to the goal active when it was made.
    assert _decide(client, guest_headers, pending, "RESISTED").status_code == 200
    assert client.get(P + "/goals/current", headers=guest_headers).json()["saved_amount"] == 0
    assert _decide(client, guest_headers, impulse, "CANCELLED").status_code == 200
    assert client.get(P + "/goals/current", headers=guest_headers).json()["saved_amount"] == 0
    assert client.get(P + "/wallet", headers=guest_headers).json()["spent_amount"] == 0


def test_wallet_expenses_purchase_and_impulse_deduct_once(client, guest_headers, today, session):
    _budget(client, guest_headers, savings=100000)
    assert client.get(P + "/wallet", headers=guest_headers).json()["remaining_amount"] == 200000
    item = create_item(client, guest_headers)
    for _ in range(2):
        response = client.put(
            f"{P}/items/{item['id']}/decision",
            headers=guest_headers,
            json={"decision": "BUY", "actual_price": 27000},
        )
        assert response.status_code == 200
    impulse = _impulse(client, guest_headers, amount=23000)
    for _ in range(2):
        assert _decide(client, guest_headers, impulse, "PURCHASED").status_code == 200
    headers = {**guest_headers, "Idempotency-Key": "manual-expense-1"}
    body = {"name": "인형뽑기", "amount": 10000, "spent_on": str(today.value)}
    expense = client.post(P + "/expenses", headers=headers, json=body).json()
    assert client.post(P + "/expenses", headers=headers, json=body).json()["id"] == expense["id"]
    assert session.scalar(select(func.count(Expense.id))) == 3
    wallet = client.get(P + "/wallet", headers=guest_headers).json()
    assert (wallet["spent_amount"], wallet["remaining_amount"], wallet["planned_savings"]) == (
        60000,
        140000,
        100000,
    )
    comparison = client.get(P + "/wallet/compare?price=150000", headers=guest_headers).json()
    assert comparison["over_budget_amount"] == 10000
    url = f"{P}/expenses/{expense['id']}"
    assert client.put(url, headers=guest_headers, json={**body, "amount": 20000}).status_code == 200
    assert client.get(P + "/wallet", headers=guest_headers).json()["spent_amount"] == 70000
    for _ in range(2):
        assert client.delete(url, headers=guest_headers).status_code == 204
    assert client.get(P + "/wallet", headers=guest_headers).json()["spent_amount"] == 50000
    automatic = session.scalar(select(Expense).where(Expense.source == "PURCHASE"))
    assert client.delete(f"{P}/expenses/{automatic.id}", headers=guest_headers).status_code == 409
    today.value = date(2026, 11, 1)
    assert client.get(P + "/wallet", headers=guest_headers).json()["needs_monthly_confirmation"] is True
    assert client.get(P + "/wallet", headers=guest_headers).json()["spent_amount"] == 0


@pytest.mark.parametrize("amount,expected", [(1, 1), (250, 3), (23000, 230)])
def test_design_rounding_up(amount, expected):
    assert compare_spending(amount, "2026-10", income=6200, savings=3100).delay_days == expected


@pytest.mark.parametrize(
    "frequency,low,high,estimate",
    [("ALMOST_DAILY", 10, 14, 12), ("OFTEN", 5, 9, 7), ("SOMETIMES", 1, 4, 2.5), ("NEVER", 0, 0, 0)],
)
def test_review_day_ranges_report_and_calibration(
    client, guest_headers, today, frequency, low, high, estimate
):
    item = create_item(client, guest_headers)
    sid = item["simulation"]["id"]
    for day in (1, 2, 3):
        today.value = date(2026, 11, day)
        answer(client, guest_headers, sid, today.value)
    today.value += timedelta(days=1)
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    today.value += timedelta(days=16)
    url = f"{P}/purchases/{purchase['id']}/review"
    response = client.put(url, headers=guest_headers, json={"frequency": frequency})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["review"]["min_days"] == low and data["review"]["max_days"] == high
    assert data["review"]["used_days"] is None and data["review"]["late_days"] == 2
    assert data["actual_days_estimate"] == estimate and data["expected_days_in_review_window"] == 14
    assert data["estimate_method"] == "range_midpoint" and data["is_estimate"]
    assert data["difference_percentage_points"] == round((1 - estimate / 14) * 100, 2)
    report = client.get(P + "/reports?month=2026-11", headers=guest_headers).json()
    assert report["categories"][0]["actual_days_estimate"] == estimate
    assert report["categories"][0]["sample_count"] == 1
    assert report["categories"][0]["overprediction_ratio_estimate"] == (
        round(14 / estimate, 2) if estimate else None
    )
    # The latest one-time review policy accepts an identical retry, not a revision.
    assert client.put(url, headers=guest_headers, json={"frequency": frequency}).status_code == 200
    different = "ALMOST_DAILY" if frequency == "NEVER" else "NEVER"
    assert client.put(url, headers=guest_headers, json={"frequency": different}).status_code == 409
    unchanged = client.get(P + "/reports?month=2026-11", headers=guest_headers).json()["categories"][0]
    assert unchanged["actual_days_estimate"] == estimate and unchanged["sample_count"] == 1
    next_item = create_item(client, guest_headers, key="next-review-category")
    today.value += timedelta(days=1)
    cards = client.get(P + "/today", headers=guest_headers).json()["questions"]
    card = next(card for card in cards if card["item_id"] == next_item["id"])
    if not estimate or 14 / estimate >= 2:
        assert card["calibration"]["sample_count"] == 1
        assert "구체적인" in card["question"]
    else:
        assert card["calibration"] is None


def test_reason_rates_overlap_and_pending_are_not_abandonments(client, guest_headers, today):
    def register(key):
        return client.post(
            P + "/items/batch",
            headers={**guest_headers, "Idempotency-Key": key},
            json={
                "items": [
                    {
                        "name": key,
                        "price": 10000,
                        "category": "OTHER",
                        "purchase_reasons": ["ADVERTISEMENT", "STRESS"],
                    }
                ]
            },
        ).json()["items"][0]

    bye, buy, pending = [register(key) for key in ("reason-bye", "reason-buy", "reason-pending")]
    today.value = date(2026, 11, 4)
    for item, decision in ((bye, "GIVE_UP"), (buy, "BUY")):
        assert (
            client.put(
                f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": decision}
            ).status_code
            == 200
        )
    result = client.get(f"{P}/simulations/{pending['simulation']['id']}/result", headers=guest_headers).json()
    assert len(result["reason_history"]) == 2
    assert all(
        row["abandonment_rate"] == 0.5 and row["decided_count"] == 2 and row["registered_count"] == 3
        for row in result["reason_history"]
    )


def test_empty_home_and_guest_isolation(client, guest_headers, today):
    empty = client.get(P + "/home", headers=guest_headers).json()
    assert empty["goal"] is None and empty["wallet"]["budget_required"]
    assert empty["simulations"] == []
    assert client.get(P + "/reports", headers=guest_headers).json()["empty"]
    expense = client.post(
        P + "/expenses",
        headers={**guest_headers, "Idempotency-Key": "private-expense"},
        json={"name": "간식", "amount": 4500, "spent_on": str(today.value)},
    ).json()
    other = client.post(P + "/guest-sessions").json()
    headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.delete(f"{P}/expenses/{expense['id']}", headers=headers).status_code == 404
    assert client.get(P + "/expenses", headers=headers).json()["items"] == []
    assert client.get(P + "/wallet?month=9999-12", headers=headers).status_code == 422
