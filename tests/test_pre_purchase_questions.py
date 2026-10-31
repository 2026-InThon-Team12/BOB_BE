import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select

from app.api.pre_purchase import get_pre_question_ai
from app.core.config import Settings
from app.integrations.lablup import LablupClient
from app.models import DailyAnswer, Purchase, SimulationQuestion, SimulationQuestionAnswer
from app.services.pre_purchase_questions import PreQuestionAI
from app.services.pre_question_catalog import BY_CODE
from app.workers.notifications import run_once
from app.core.clock import SEOUL
from tests.test_core import create_item
from tests.test_purchase_questions import use_ai as use_post_ai, ai_response as post_response
from tests.test_notifications import FakeSender

P = "/api/v1"


def response_for(code, wording=None):
    return httpx.Response(
        200,
        json={
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "template_code": code,
                            "question_text": wording or BY_CODE[code].wording.format(item="운동화"),
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        },
    )


def use_ai(app, code=None, handler=None):
    config = Settings(_env_file=None, lablup_api_key="test-pre-purchase")
    calls = []

    def wrapped(request):
        calls.append(json.loads(request.content))
        return handler(request) if handler else response_for(code)

    client = LablupClient(config, httpx.Client(transport=httpx.MockTransport(wrapped)))
    ai = PreQuestionAI(config, client)
    app.dependency_overrides[get_pre_question_ai] = lambda: ai
    return calls


def generate(client, headers, item):
    response = client.post(f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, headers, question, **body):
    return client.put(f"{P}/simulation-questions/{question['id']}/answer", headers=headers, json=body)


def result(client, headers, item):
    return client.get(f"{P}/simulations/{item['simulation']['id']}/result", headers=headers).json()


def test_explicit_server_adaptive_mode_does_not_call_ai_in_reads(
    client, guest_headers, today, app, session_factory
):
    created = client.post(
        P + "/items/batch",
        headers={**guest_headers, "Idempotency-Key": "new-default-mode"},
        json={
            "items": [
                {
                    "name": "무드등",
                    "price": 29000,
                    "category": "FURNITURE",
                    "purchase_reasons": ["LONG_WANTED"],
                    "question_mode": "ADAPTIVE",
                }
            ]
        },
    ).json()["items"][0]
    assert created["simulation"]["question_mode"] == "ADAPTIVE"
    assert len(client.get(P + "/pre-purchase-question-templates").json()["templates"]) == 14
    sid = created["simulation"]["id"]
    assert client.post(f"{P}/simulations/{sid}/daily-question", headers=guest_headers).status_code == 409
    calls = use_ai(app, "DISCOVERY")
    today.value += timedelta(days=1)
    cards = client.get(P + "/home", headers=guest_headers).json()["today"]
    assert cards["questions"] == [] and len(cards["pre_purchase_questions"]) == 1
    assert cards["summary"]["remaining_count"] == 1
    assert cards["pre_purchase_questions"][0]["question"] is None
    assert calls == []
    client.put(P + "/me/push-token", headers=guest_headers, json={"token": "adaptive-pre-device"})
    sent = run_once(
        session_factory,
        FakeSender(),
        now=datetime.combine(today.value, datetime.min.time(), tzinfo=SEOUL).replace(hour=20),
        settings=Settings(_env_file=None),
    )
    assert sent.sent == 1 and calls == []
    assert (
        client.put(
            f"{P}/simulations/{sid}/answers/{today.value}",
            headers=guest_headers,
            json={"usage_answer": "YES"},
        ).status_code
        == 409
    )


def test_reflection_answers_do_not_become_usage_and_forecast_compares_after_purchase(
    app, client, guest_headers, today, session
):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    for code, values in (
        ("CURRENT_MOOD", {"choice_code": "HAPPY"}),
        ("RECENT_NEED", {"choice_code": "YES"}),
        ("EXPECTED_USE_DAYS", {"number_value": 15}),
    ):
        today.value += timedelta(days=1)
        calls = use_ai(app, code)
        question = generate(client, guest_headers, item)
        assert question == generate(client, guest_headers, item) and len(calls) == 1
        assert submit(client, guest_headers, question, **values).status_code == 200
        assert submit(client, guest_headers, question, **values).status_code == 200
        if code != "EXPECTED_USE_DAYS":
            assert result(client, guest_headers, item)["expected_used_days"] is None
    assert session.scalar(select(func.count(DailyAnswer.id))) == 0
    assert session.scalar(select(func.count(SimulationQuestionAnswer.id))) == 3
    today.value += timedelta(days=1)
    output = result(client, guest_headers, item)
    assert output["expected_used_days"] == 15 and output["prediction_window_days"] == 30
    assert output["unused_days"] is None and output["expected_usage_rate"] == 0.5
    assert output["expected_cost_per_used_day"] == 2000 and output["comparison_eligible"]
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    assert purchase["pre_purchase_baseline"]["forecast"]["expected_used_days"] == 15
    snapshot = session.get(Purchase, purchase["id"]).pre_purchase_snapshot
    assert snapshot["forecast"]["expected_used_days"] == 15
    today.value += timedelta(days=7)
    use_post_ai(app, lambda request: post_response("USAGE_DAYS"))
    q = client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=guest_headers).json()
    checked = client.put(
        f"{P}/purchase-questions/{q['id']}/answer", headers=guest_headers, json={"choice_code": "D2"}
    ).json()
    assert checked["comparison"]["expected_used_days"] == 3.5
    assert checked["comparison"]["difference_percentage_points"] == 21.43
    category = client.get(P + "/reports?month=2026-11", headers=guest_headers).json()["categories"][0]
    assert category["predicted_days"] == 3.5 and category["actual_days_estimate"] == 2


def test_problem_and_first_use_are_frozen_at_purchase_and_used_in_followup(
    app, client, guest_headers, today, session
):
    item = create_item(client, guest_headers, price=70000, mode="ADAPTIVE")
    today.value += timedelta(days=2)
    use_ai(app, "PROBLEM_TO_SOLVE")
    problem = generate(client, guest_headers, item)
    sentence = "걷는 동안 발이 아파서 편하게 이동하고 싶어요."
    assert submit(client, guest_headers, problem, text_value=sentence).status_code == 200
    today.value += timedelta(days=1)
    use_ai(app, "FIRST_USE")
    first = generate(client, guest_headers, item)
    assert (
        submit(
            client, guest_headers, first, object_value={"when": "THIS_WEEK", "context": "EXERCISE"}
        ).status_code
        == 200
    )
    for obj in (
        {"when": "THIS_WEEK"},
        {"when": "NOW", "context": "HOME"},
        {"when": "TODAY", "context": "HOME", "extra": "x"},
    ):
        assert submit(client, guest_headers, first, object_value=obj).status_code == 422
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    assert (
        submit(client, guest_headers, first, object_value={"when": "TODAY", "context": "HOME"}).status_code
        == 409
    )
    assert purchase["pre_purchase_baseline"]["problem_to_solve"] == sentence
    assert purchase["pre_purchase_baseline"]["first_use"]["context"] == "EXERCISE"
    today.value += timedelta(days=8)
    use_post_ai(app, lambda request: post_response("USAGE_CONTEXT"))
    q = client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=guest_headers).json()
    actual = client.put(
        f"{P}/purchase-questions/{q['id']}/answer", headers=guest_headers, json={"choice_code": "EXERCISE"}
    ).json()
    assert actual["comparison"]["matches_prediction"] is True
    today.value += timedelta(days=30)
    use_post_ai(app, lambda request: post_response("PROBLEM_SOLVED"))
    q = client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=guest_headers).json()
    assert q["reference_text"] == "구매 전에 해결하고 싶었던 문제: " + sentence
    assert q["context"]["problem_to_solve"] == sentence


@pytest.mark.parametrize("value", [0, 30, None])
def test_forecast_zero_and_unknown_are_distinct_and_last_day_requires_forecast(
    app, client, guest_headers, today, value
):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=3)
    # AI cannot substitute an unrelated question for the last missing forecast.
    calls = use_ai(app, "SHOW_TO")
    question = generate(client, guest_headers, item)
    assert question["template_code"] == "EXPECTED_USE_DAYS" and len(calls) == 2
    assert question["generation_method"] == "TEMPLATE"
    payload = {"number_value": value} if value is not None else {"choice_code": "UNSURE"}
    assert submit(client, guest_headers, question, **payload).status_code == 200
    for bad in (
        {"number_value": True},
        {"number_value": 31},
        {"number_value": -1},
        {"number_value": 1.1},
        {"text_value": "15"},
        {"choice_code": "YES"},
        {"number_value": 0, "choice_code": "UNSURE"},
    ):
        assert submit(client, guest_headers, question, **bad).status_code == 422
    today.value += timedelta(days=1)
    output = result(client, guest_headers, item)
    assert output["expected_used_days"] == value
    assert output["expected_cost_per_used_day"] == (1000 if value == 30 else None)
    assert output["comparison_reason"] == ("FORECAST_REQUIRED" if value is None else "INSUFFICIENT_ANSWERS")


def test_hourly_wage_and_goal_delay_are_server_values_and_not_inferred(app, client, guest_headers, today):
    item = create_item(client, guest_headers, price=70000, mode="ADAPTIVE")
    today.value += timedelta(days=4)
    calls = use_ai(app, "WORK_VALUE")
    q = generate(client, guest_headers, item)
    payload = json.loads(calls[0]["messages"][0]["content"][0]["text"])
    assert {row["template_code"] for row in payload["eligible_templates"]} == {"CASH_CHOICE"}
    assert q["context"]["work_hours_estimate"] is None
    second = create_item(client, guest_headers, price=70000, key="with-explicit-budget", mode="ADAPTIVE")
    budget = {
        "income": 1000000,
        "food": 200000,
        "living": 200000,
        "transport": 100000,
        "savings": 150000,
        "hourly_wage": 10000,
    }
    assert client.put(P + "/budgets/2026-11", headers=guest_headers, json=budget).status_code == 200
    assert (
        client.put(
            P + "/budgets/2026-11", headers=guest_headers, json={**budget, "hourly_wage": 0}
        ).status_code
        == 422
    )
    client.post(P + "/goals", headers=guest_headers, json={"name": "미쿠 피규어", "target_amount": 240000})
    today.value += timedelta(days=4)
    use_ai(app, "WORK_VALUE")
    q = generate(client, guest_headers, second)
    assert q["context"]["work_hours_estimate"] == 7
    assert "약 7시간" in q["reference_text"]
    assert q["context"]["goal_delay_days"] == 14
    client.put(P + "/budgets/2026-11", headers=guest_headers, json={**budget, "hourly_wage": 20000})
    assert generate(client, guest_headers, second) == q


def test_pre_question_ownership_replay_and_late_answer(app, client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)
    use_ai(app, "CURRENT_MOOD")
    q = generate(client, guest_headers, item)
    assert submit(client, guest_headers, q, choice_code="STRESS").status_code == 200
    tasks = client.get(P + "/today", headers=guest_headers).json()
    assert tasks["pre_purchase_questions"] == [] and len(tasks["pre_purchase_answered"]) == 1
    assert (
        len(
            client.get(P + "/today?include_answered=true", headers=guest_headers).json()[
                "pre_purchase_questions"
            ]
        )
        == 1
    )
    other = client.post(P + "/guest-sessions").json()
    headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(f"{P}/simulation-questions/{q['id']}", headers=headers).status_code == 404
    assert submit(client, headers, q, choice_code="HAPPY").status_code == 404
    assert (
        client.get(f"{P}/simulations/{item['simulation']['id']}/questions", headers=headers).status_code
        == 404
    )
    assert (
        client.post(f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=headers).status_code
        == 404
    )
    today.value += timedelta(days=1)
    assert submit(client, guest_headers, q, choice_code="HAPPY").status_code == 409


def test_concurrent_generation_and_answer_have_one_record(app, client, guest_headers, today, session):
    if session.bind.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL")
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)
    barrier = Barrier(2)

    def handler(request):
        barrier.wait(timeout=10)
        return response_for("CURRENT_MOOD")

    use_ai(app, handler=handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: generate(client, guest_headers, item), range(2)))
    assert responses[0] == responses[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(
            pool.map(lambda _: submit(client, guest_headers, responses[0], choice_code="HAPPY"), range(2))
        )
    assert all(response.status_code == 200 for response in answers)
    assert session.scalar(select(func.count(SimulationQuestion.id))) == 1
    assert session.scalar(select(func.count(SimulationQuestionAnswer.id))) == 1


def test_purchase_during_ai_generation_blocks_late_question(app, client, guest_headers, today, session):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)

    def handler(request):
        assert (
            client.put(
                f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
            ).status_code
            == 200
        )
        return response_for("DISCOVERY")

    use_ai(app, handler=handler)
    response = client.post(
        f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=guest_headers
    )
    assert response.status_code == 409
    assert session.scalar(select(func.count(SimulationQuestion.id))) == 0


def test_hold_gaps_do_not_generate_pre_questions_and_preserve_history(app, client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=3)
    use_ai(app, "EXPECTED_USE_DAYS")
    q = generate(client, guest_headers, item)
    assert submit(client, guest_headers, q, number_value=12).status_code == 200
    today.value += timedelta(days=10)
    assert (
        client.post(
            f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=guest_headers
        ).status_code
        == 409
    )
    held = client.put(
        f"{P}/items/{item['id']}/decision",
        headers={**guest_headers, "Idempotency-Key": "pre-hold-001"},
        json={"decision": "HOLD"},
    )
    assert held.status_code == 200
    assert client.get(P + "/today", headers=guest_headers).json()["pre_purchase_questions"] == []
    today.value += timedelta(days=1)
    use_ai(app, "CASH_CHOICE")
    q2 = generate(client, guest_headers, item)
    assert q2["context"]["observation_day"] == 4
    history = client.get(
        f"{P}/simulations/{item['simulation']['id']}/questions", headers=guest_headers
    ).json()
    assert len(history["items"]) == 2 and history["baseline"]["forecast"]["expected_used_days"] == 12


def test_uncertain_reflection_does_not_satisfy_comparison_coverage(app, client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    for code, body in (
        ("CURRENT_MOOD", {"choice_code": "NORMAL"}),
        ("RECENT_NEED", {"choice_code": "UNSURE"}),
        ("EXPECTED_USE_DAYS", {"number_value": 0}),
    ):
        today.value += timedelta(days=1)
        use_ai(app, code)
        q = generate(client, guest_headers, item)
        assert submit(client, guest_headers, q, **body).status_code == 200
    today.value += timedelta(days=1)
    output = result(client, guest_headers, item)
    assert output["answered_days"] == 3 and output["definite_answer_days"] == 2
    assert output["comparison_reason"] == "INSUFFICIENT_ANSWERS"
    assert output["expected_usage_rate"] == 0 and output["unused_days"] is None


def test_forecast_unit_correction_and_timeout_fallback(app, client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=3)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return response_for(
            "EXPECTED_USE_DAYS",
            "운동화를 하루에 몇 번 신을까요?"
            if count == 1
            else "산 뒤 30일 동안 운동화를 며칠 신을 것 같아요?",
        )

    use_ai(app, handler=handler)
    q = generate(client, guest_headers, item)
    assert q["generation_method"] == "AI" and count == 2
    second = create_item(client, guest_headers, mode="ADAPTIVE", key="timeout-pre-question")
    today.value += timedelta(days=1)

    def timeout(request):
        raise httpx.ReadTimeout("sensitive upstream response")

    calls = use_ai(app, handler=timeout)
    q = generate(client, guest_headers, second)
    assert len(calls) == 1 and q["fallback_reason"] == "AI_TIMEOUT"
    assert q["generation_method"] == "TEMPLATE" and "sensitive" not in json.dumps(q)
