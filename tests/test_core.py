from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models import DailyAnswer, Guest, Item, Purchase, RegistrationBatch
from app.services.simulations import duration_for_price

PREFIX = "/api/v1"


def create_item(client, headers, price=30_000, key="registration-1", mode="LEGACY"):
    response = client.post(
        PREFIX + "/items/batch",
        headers={**headers, "Idempotency-Key": key},
        json={
            "items": [
                {
                    "name": "운동화",
                    "price": price,
                    "category": "CLOTHING",
                    "purchase_reason": "NECESSITY",
                    "question_mode": mode,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["items"][0]


def answer(client, headers, simulation_id, day, value="YES"):
    return client.put(
        f"{PREFIX}/simulations/{simulation_id}/answers/{day.isoformat()}",
        headers=headers,
        json={"usage_answer": value, "supplemental_answer": "UNSURE"},
    )


@pytest.mark.parametrize("price,days", [(1, 3), (30000, 3), (30001, 7), (99999, 7), (100000, 14)])
def test_price_boundaries(price, days):
    assert duration_for_price(price) == days


def test_seoul_midnight(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc))
    assert clock.kst_today() == date(2026, 10, 31)
    monkeypatch.setattr(clock, "utc_now", lambda: datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))
    assert clock.kst_today() == date(2026, 11, 1)


def test_guest_hash_and_ownership(client, session):
    response = client.post(PREFIX + "/guest-sessions").json()
    headers = {"Authorization": "Bearer " + response["access_token"]}
    guest = session.get(Guest, response["guest_id"])
    assert guest.token_hash != response["access_token"] and len(guest.token_hash) == 64
    item = create_item(client, headers)
    other = client.post(PREFIX + "/guest-sessions").json()
    other_headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(PREFIX + f"/items/{item['id']}", headers=other_headers).status_code == 404
    assert client.get(PREFIX + "/items").status_code == 401
    assert (
        client.get(
            PREFIX + f"/simulations/{item['simulation']['id']}/result", headers=other_headers
        ).status_code
        == 404
    )


def test_registration_idempotency_and_invalid_money(client, guest_headers, session):
    first = create_item(client, guest_headers)
    second = create_item(client, guest_headers)
    assert first == second
    assert session.scalar(select(func.count(Item.id))) == 1
    response = client.post(
        PREFIX + "/items/batch",
        headers={**guest_headers, "Idempotency-Key": "registration-1"},
        json={"items": [{"name": "다름", "price": 10, "category": "OTHER", "purchase_reason": "STRESS"}]},
    )
    assert response.status_code == 409
    for price in (0, -1, 1.5, True, "30000"):
        response = client.post(
            PREFIX + "/items/batch",
            headers={**guest_headers, "Idempotency-Key": "invalid-money"},
            json={
                "items": [{"name": "검증", "price": price, "category": "OTHER", "purchase_reason": "STRESS"}]
            },
        )
        assert response.status_code == 422


def test_full_simulation_purchase_review_loop(client, guest_headers, today, session):
    item = create_item(client, guest_headers)
    sid = item["simulation"]["id"]
    assert item["simulation"]["start_date"] == "2026-11-01"
    assert client.get(PREFIX + "/today", headers=guest_headers).json()["questions"] == []
    for day, value in [(1, "YES"), (2, "YES"), (3, "NO")]:
        today.value = date(2026, 11, day)
        assert len(client.get(PREFIX + "/today", headers=guest_headers).json()["questions"]) == 1
        assert answer(client, guest_headers, sid, today.value, value).status_code == 200
        assert answer(client, guest_headers, sid, today.value, value).status_code == 200
        assert client.get(PREFIX + "/today", headers=guest_headers).json()["questions"] == []
    assert session.scalar(select(func.count(DailyAnswer.id))) == 3
    today.value = date(2026, 11, 4)
    result = client.get(f"{PREFIX}/simulations/{sid}/result", headers=guest_headers).json()
    assert result["expected_used_days"] == 2
    assert result["expected_cost_per_used_day"] == 15000
    assert result["comparison_eligible"] is True
    assert answer(client, guest_headers, sid, date(2026, 11, 3)).status_code == 409
    decision = client.put(
        f"{PREFIX}/items/{item['id']}/decision",
        headers=guest_headers,
        json={"decision": "BUY", "actual_price": 27000},
    ).json()
    purchase = decision["purchase"]
    assert purchase["review_due_on"] == "2026-11-18"
    review_url = f"{PREFIX}/purchases/{purchase['id']}/review"
    assert client.put(review_url, headers=guest_headers, json={"used_days": 2}).status_code == 409
    today.value = date(2026, 11, 20)
    review = client.put(review_url, headers=guest_headers, json={"used_days": 2}).json()
    assert review["review_start"] == "2026-11-04" and review["review_end"] == "2026-11-17"
    assert review["review"]["late_days"] == 2
    assert review["difference_percentage_points"] == 52.38
    assert client.get(PREFIX + "/today", headers=guest_headers).json()["reviews"] == []
    assert session.scalar(select(func.count(Purchase.id))) == 1


def test_unknown_missing_not_unused_and_zero_cost(client, guest_headers, today):
    item = create_item(client, guest_headers)
    sid = item["simulation"]["id"]
    today.value = date(2026, 11, 1)
    answer(client, guest_headers, sid, today.value, "UNSURE")
    today.value = date(2026, 11, 2)
    answer(client, guest_headers, sid, today.value, "NO")
    today.value = date(2026, 11, 4)
    result = client.get(f"{PREFIX}/simulations/{sid}/result", headers=guest_headers).json()
    assert (result["unsure_days"], result["unanswered_days"], result["unused_days"]) == (1, 1, 1)
    assert result["expected_cost_per_used_day"] is None
    assert result["comparison_reason"] == "INSUFFICIENT_ANSWERS"


def test_early_purchase_cancels_questions_and_excludes_comparison(client, guest_headers, today):
    item = create_item(client, guest_headers, price=100000)
    sid = item["simulation"]["id"]
    today.value = date(2026, 11, 1)
    answer(client, guest_headers, sid, today.value)
    purchase = client.put(
        f"{PREFIX}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    assert answer(client, guest_headers, sid, today.value).status_code == 409
    assert not client.get(PREFIX + "/today", headers=guest_headers).json()["questions"]
    result = client.get(f"{PREFIX}/simulations/{sid}/result", headers=guest_headers).json()
    assert result["comparison_reason"] == "EARLY_PURCHASE"
    assert result["unanswered_days"] == 0 and result["pending_days"] == 0
    today.value += timedelta(days=14)
    reviewed = client.put(
        f"{PREFIX}/purchases/{purchase['id']}/review", headers=guest_headers, json={"used_days": 0}
    ).json()
    assert reviewed["difference_percentage_points"] is None


def test_edit_freeze_and_hold(client, guest_headers, today):
    item = create_item(client, guest_headers)
    url = f"{PREFIX}/items/{item['id']}"
    changed = client.patch(url, headers=guest_headers, json={"price": 100000}).json()
    assert changed["simulation"]["duration_days"] == 14
    assert client.put(url + "/decision", headers=guest_headers, json={"decision": "HOLD"}).status_code == 409
    today.value = date(2026, 11, 1)
    assert client.patch(url, headers=guest_headers, json={"price": 100}).status_code == 409
    today.value = date(2026, 11, 15)
    held = client.put(
        url + "/decision",
        headers={**guest_headers, "Idempotency-Key": "hold-once-only"},
        json={"decision": "HOLD"},
    )
    assert held.status_code == 200
    assert held.json()["simulation"]["duration_days"] == 21
    assert client.get(PREFIX + "/today", headers=guest_headers).json()["decisions"] == []
    today.value = date(2026, 11, 23)
    assert len(client.get(PREFIX + "/today", headers=guest_headers).json()["decisions"]) == 1
    assert (
        client.put(url + "/decision", headers=guest_headers, json={"decision": "GIVE_UP"}).status_code == 200
    )
    assert client.get(PREFIX + "/today", headers=guest_headers).json()["decisions"] == []


def test_push_token_lifecycle_and_redaction(client, guest_headers):
    token = "private-device-token-123"
    assert client.put(PREFIX + "/me/push-token", headers=guest_headers, json={"token": token}).json() == {
        "enabled": True
    }
    other = client.post(PREFIX + "/guest-sessions").json()
    other_headers = {"Authorization": "Bearer " + other["access_token"]}
    assert (
        client.put(PREFIX + "/me/push-token", headers=other_headers, json={"token": token}).status_code == 409
    )
    assert client.delete(PREFIX + "/me/push-token", headers=guest_headers).status_code == 204
    invalid = client.put(PREFIX + "/me/push-token", headers=guest_headers, json={"token": "secret"})
    assert invalid.status_code == 422 and "secret" not in invalid.text


def test_concurrent_batch_retry(client, guest_headers, session_factory):
    if session_factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_item(client, guest_headers), range(2)))
    assert results[0]["id"] == results[1]["id"]
    with session_factory() as session:
        assert session.scalar(select(func.count(RegistrationBatch.id))) == 1
