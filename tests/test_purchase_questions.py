from datetime import timedelta

import pytest

from app.models import Item, SimulationQuestion, SimulationQuestionAnswer
from tests.test_core import create_item
from tests.test_handoff import register

P = "/api/v1"


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("code,low,high", [
    ("DAYS_1_3", 1, 3), ("DAYS_4_7", 4, 7),
    ("DAYS_8_10", 8, 10), ("DAYS_11_14", 11, 14),
])
def test_one_time_usage_review(client, guest_headers, today, local, code, low, high):
    item = register(client, guest_headers).json()["items"][0] if local else create_item(client, guest_headers)
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    url = f"{P}/purchases/{purchase['id']}/review"
    bought = today.value
    for age in (0, 13):
        today.value = bought + timedelta(days=age)
        daily = client.get(P + "/today", headers=guest_headers).json()
        assert daily["reviews"] == daily["purchase_questions"] == []
        assert client.put(url, headers=guest_headers, json={"frequency": code}).status_code == 409
    today.value = bought + timedelta(days=14)
    daily = client.get(P + "/today", headers=guest_headers).json()
    assert daily["purchase_questions"] == []
    assert len(daily["reviews"]) == 1
    assert [o["value"] for o in daily["reviews"][0]["review_options"]] == [
        "DAYS_1_3", "DAYS_4_7", "DAYS_8_10", "DAYS_11_14"
    ]
    path = "local-question" if local else "daily-question"
    assert client.post(f"{P}/purchases/{purchase['id']}/{path}", headers=guest_headers, json={}).status_code == 409
    response = client.put(url, headers=guest_headers, json={"frequency": code})
    assert response.status_code == 200, response.text
    review = response.json()["review"]
    assert (review["min_days"], review["max_days"], review["estimated_days"]) == (low, high, (low+high)/2)
    assert review["used_days"] is None
    report = client.get(P + "/reports", headers=guest_headers).json()
    assert len(report["categories"]) == 1
    category = report["categories"][0]
    assert (category["actual_days_min"], category["actual_days_max"]) == (low, high)
    assert category["actual_days_estimate"] == (low + high) / 2
    assert category["sample_count"] == 1
    assert category["predicted_days"] is None
    assert category["status"] == "PREDICTION_UNAVAILABLE"
    assert report["discovery"] is None
    assert client.put(url, headers=guest_headers, json={"frequency": code}).json()["review"] == review
    other = "DAYS_1_3" if code != "DAYS_1_3" else "DAYS_11_14"
    assert client.put(url, headers=guest_headers, json={"frequency": other}).status_code == 409
    for age in (14, 15, 45):
        today.value = bought + timedelta(days=age)
        daily = client.get(P + "/today", headers=guest_headers).json()
        assert daily["reviews"] == daily["purchase_questions"] == []


def test_historical_numeric_question_and_baseline_are_not_reinterpreted(
    client, guest_headers, today, session
):
    from app.services.pre_question_catalog import BY_CODE

    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)
    q = SimulationQuestion(
        guest_id=session.get(Item, item["id"]).guest_id,
        simulation_id=item["simulation"]["id"],
        question_date=today.value,
        template_code="EXPECTED_USE_DAYS",
        template_version="pre-v1",
        stage="PREDICTION",
        question_text="운동화를 산 뒤 30일 동안 며칠 사용할까요?",
        answer_spec=BY_CODE["EXPECTED_USE_DAYS"].answer_spec,
        context_snapshot={},
        generation_method="TEMPLATE",
    )
    session.add(q)
    session.flush()
    session.add(SimulationQuestionAnswer(question_id=q.id, number_value=12, answered_on=today.value))
    session.commit()
    snapshot = client.get(f"{P}/simulation-questions/{q.id}", headers=guest_headers).json()
    assert snapshot["answer"]["number_value"] == 12 and snapshot["answer_spec"]["type"] == "INTEGER"
    history = client.get(
        f"{P}/simulations/{item['simulation']['id']}/questions", headers=guest_headers
    ).json()
    assert history["baseline"]["forecast"]["expected_used_days"] == 12
