from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Item, PurchaseQuestionAnswer, SimulationQuestion, SimulationQuestionAnswer
from app.services.purchase_questions import stages_for, month_anniversary
from app.services import swipe_questions as swipe
from tests.test_core import create_item, answer
from tests.test_handoff import register

P = "/api/v1"


def purchase_item(client, headers, today, complete=False):
    item = create_item(client, headers)
    if complete:
        for value in ("YES", "YES", "NO"):
            today.value += timedelta(days=1)
            assert answer(client, headers, item["simulation"]["id"], today.value, value).status_code == 200
        today.value += timedelta(days=1)
    response = client.put(f"{P}/items/{item['id']}/decision", headers=headers, json={"decision": "BUY"})
    assert response.status_code == 200
    return response.json()["purchase"]


def generate(client, headers, purchase):
    response = client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("offset,stages", [(-1, []), (0, ["IMMEDIATE"]), (3, ["IMMEDIATE"]), (4, ["USAGE"])])
def test_old_phase_metadata_stays_readable(offset, stages):
    assert stages_for(date(2026, 1, 31), date(2026, 1, 31) + timedelta(days=offset)) == stages
    assert month_anniversary(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_all_new_post_questions_are_swipes_and_repeat_is_frozen(client, guest_headers, today, session):
    purchase = purchase_item(client, guest_headers, today)
    today.value += timedelta(days=14)
    seen = []
    for day in range(10):
        q = generate(client, guest_headers, purchase)
        assert q["template_version"] == swipe.VERSION
        assert q["answer_spec"] == swipe.ANSWER_SPEC
        assert q["template_code"] not in seen[-3:]
        assert generate(client, guest_headers, purchase) == q
        for choice in ("YES", "UNSURE", "NO"):
            assert (
                client.put(
                    f"{P}/purchase-questions/{q['id']}/answer",
                    headers=guest_headers,
                    json={"choice_code": choice},
                ).status_code
                == 200
            )
        assert (
            client.put(
                f"{P}/purchase-questions/{q['id']}/answer", headers=guest_headers, json={"number_value": 5}
            ).status_code
            == 422
        )
        seen.append(q["template_code"])
        today.value += timedelta(days=1)
    assert len(set(seen[:8])) == 8
    assert session.scalar(select(func.count(PurchaseQuestionAnswer.id))) == 10
    history = client.get(f"{P}/purchases/{purchase['id']}/questions", headers=guest_headers).json()
    assert history["usage_summary"]["observed_days"] == 0
    assert history["usage_summary"]["reported_used_days"] == 0


def test_local_post_uses_three_choices_without_a_separate_frequency_survey(client, guest_headers, today):
    item = register(client, guest_headers).json()["items"][0]
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    assert purchase["question_mode"] == "LOCAL"
    today.value += timedelta(days=14)
    daily = client.get(P + "/today", headers=guest_headers).json()
    assert daily["reviews"] == []
    assert len(daily["purchase_questions"]) == 1
    history = []
    for day in range(8):
        q = client.post(
            f"{P}/purchases/{purchase['id']}/local-question",
            headers=guest_headers,
            json={"recent_templates": history},
        ).json()
        assert q["answer_spec"] == swipe.ANSWER_SPEC
        assert q["usage_window"] is None
        assert q["template_code"] not in {r["template_code"] for r in history[-3:]}
        history.append({"date": q["question_date"], "template_code": q["template_code"]})
        today.value += timedelta(days=1)
    assert len({r["template_code"] for r in history}) == 8


def test_post_question_owner_and_date_protection(client, guest_headers, today):
    purchase = purchase_item(client, guest_headers, today)
    today.value += timedelta(days=14)
    q = generate(client, guest_headers, purchase)
    foreign = {"Authorization": "Bearer " + client.post(P + "/guest-sessions").json()["access_token"]}
    assert client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=foreign).status_code == 404
    assert (
        client.put(
            f"{P}/purchase-questions/{q['id']}/answer", headers=foreign, json={"choice_code": "YES"}
        ).status_code
        == 404
    )
    today.value += timedelta(days=1)
    assert (
        client.put(
            f"{P}/purchase-questions/{q['id']}/answer", headers=guest_headers, json={"choice_code": "YES"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("local", [False, True])
def test_purchase_reflections_wait_until_review_due_day(client, guest_headers, today, local):
    if local:
        item = register(client, guest_headers).json()["items"][0]
        purchase = client.put(
            f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
        ).json()["purchase"]
    else:
        purchase = purchase_item(client, guest_headers, today)
    bought = today.value
    path = "local-question" if local else "daily-question"
    url = f"{P}/purchases/{purchase['id']}/{path}"
    assert purchase["review_due_on"] == (bought + timedelta(days=14)).isoformat()
    for age in (0, 13):
        today.value = bought + timedelta(days=age)
        assert client.get(P + "/today", headers=guest_headers).json()["purchase_questions"] == []
        assert client.post(url, headers=guest_headers, json={}).status_code == 409
    today.value = bought + timedelta(days=14)
    assert len(client.get(P + "/today", headers=guest_headers).json()["purchase_questions"]) == 1
    assert client.post(url, headers=guest_headers, json={}).status_code == 200


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
