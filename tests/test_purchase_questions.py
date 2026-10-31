import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select

from app.api.post_purchase import get_question_ai
from app.core.clock import SEOUL
from app.core.config import Settings
from app.integrations.lablup import LablupClient
from app.models import PurchaseQuestion, PurchaseQuestionAnswer
from app.services.purchase_questions import QuestionAI, stages_for, month_anniversary
from app.services.question_catalog import BY_CODE
from app.workers.notifications import run_once
from tests.test_core import create_item, answer
from tests.test_notifications import FakeSender

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


def use_ai(app, handler):
    settings = Settings(_env_file=None, lablup_api_key="unit-test-only")
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    ai = QuestionAI(settings, LablupClient(settings, transport))
    app.dependency_overrides[get_question_ai] = lambda: ai
    return ai


def ai_response(code, wording=None):
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


def generate(client, headers, purchase):
    response = client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, headers, question, **body):
    return client.put(f"{P}/purchase-questions/{question['id']}/answer", headers=headers, json=body)


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 1, 30), []),
        (date(2026, 1, 31), ["IMMEDIATE"]),
        (date(2026, 2, 3), ["IMMEDIATE"]),
        (date(2026, 2, 4), ["USAGE"]),
        (date(2026, 2, 27), ["USAGE"]),
        (date(2026, 2, 28), ["USAGE", "VALUE", "MOTIVE"]),
        (date(2026, 4, 29), ["USAGE", "VALUE", "MOTIVE"]),
        (date(2026, 4, 30), ["USAGE", "LONG_TERM"]),
    ],
)
def test_purchase_phase_boundaries_are_calendar_months(today, expected):
    assert stages_for(date(2026, 1, 31), today) == expected
    assert month_anniversary(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_fixed_catalog_has_all_photo_questions(client):
    catalog = client.get(P + "/question-templates").json()
    assert len(catalog["templates"]) == 15
    assert len({row["code"] for row in catalog["templates"]}) == 15
    assert catalog["metric_unit"] == "usage_days"


def test_daily_ai_question_is_immutable_and_answers_update_once(app, client, guest_headers, today, session):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert str(request.url) == "https://api.console.lablup.ai/anthropic/v1/messages"
        context = json.loads(payload["messages"][0]["content"][0]["text"])
        assert context["context"]["item_name"] == "운동화"
        assert {row["template_code"] for row in context["eligible_templates"]} == {
            "EXPECTATION",
            "MOOD",
            "CHAIN_PURCHASE",
        }
        return ai_response("EXPECTATION", "운동화를 받아보니 기대했던 착용감과 비교해 어때요?")

    use_ai(app, handler)
    purchase = purchase_item(client, guest_headers, today)
    question = generate(client, guest_headers, purchase)
    assert question["generation_method"] == "AI" and question["template_code"] == "EXPECTATION"
    assert question == generate(client, guest_headers, purchase) and len(calls) == 1
    assert question["context"]["template_original"] == "받아보니 기대했던 것과 비교해 어때요?"
    assert submit(client, guest_headers, question, choice_code="ABOVE").status_code == 200
    assert submit(client, guest_headers, question, choice_code="BELOW").status_code == 200
    assert submit(client, guest_headers, question, choice_code="D7").status_code == 422
    assert session.scalar(select(func.count(PurchaseQuestion.id))) == 1
    assert session.scalar(select(func.count(PurchaseQuestionAnswer.id))) == 1
    tasks = client.get(P + "/today", headers=guest_headers).json()
    assert tasks["purchase_questions"] == [] and len(tasks["purchase_answered"]) == 1
    assert (
        client.get(P + "/today?include_answered=true", headers=guest_headers).json()["purchase_questions"][0][
            "question"
        ]["answer"]["choice_code"]
        == "BELOW"
    )
    today.value += timedelta(days=1)
    assert submit(client, guest_headers, question, choice_code="ABOVE").status_code == 409
    history = client.get(f"{P}/purchases/{purchase['id']}/questions", headers=guest_headers).json()
    assert history["items"][0]["question"] == question["question"]
    assert history["usage_summary"]["reported_used_days"] == 0
    assert history["usage_summary"]["observed_days"] == 0


def test_ai_invalid_phase_or_options_corrected_then_falls_back(app, client, guest_headers, today):
    calls = []

    def handler(request):
        calls.append(request)
        return ai_response("RESALE_VALUE")  # prohibited immediately after purchase

    use_ai(app, handler)
    purchase = purchase_item(client, guest_headers, today)
    question = generate(client, guest_headers, purchase)
    assert len(calls) == 2 and question["generation_method"] == "TEMPLATE"
    assert question["stage"] == "IMMEDIATE" and question["fallback_reason"] == "AI_INVALID_OUTPUT"
    assert "운동화" in question["question"]
    assert generate(client, guest_headers, purchase) == question and len(calls) == 2


def test_timeout_fallback_and_no_generation_in_gets_or_push(
    app, client, guest_headers, today, session_factory
):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret upstream content must not escape")

    use_ai(app, handler)
    purchase = purchase_item(client, guest_headers, today)
    assert (
        client.get(P + "/home", headers=guest_headers).json()["today"]["purchase_questions"][0]["question"]
        is None
    )
    client.put(P + "/me/push-token", headers=guest_headers, json={"token": "purchase-question-device"})
    sender = FakeSender()
    assert (
        run_once(
            session_factory,
            sender,
            now=datetime.combine(today.value, datetime.min.time(), tzinfo=SEOUL).replace(hour=20),
            settings=Settings(_env_file=None),
        ).sent
        == 1
    )
    assert calls == []
    question = generate(client, guest_headers, purchase)
    assert len(calls) == 1 and question["fallback_reason"] == "AI_TIMEOUT"
    assert "secret" not in json.dumps(question)


def test_weekly_usage_is_days_and_disjoint_not_emotion(app, client, guest_headers, today, session):
    purchase = purchase_item(client, guest_headers, today, complete=True)
    use_ai(app, lambda request: ai_response("MOOD"))
    mood = generate(client, guest_headers, purchase)
    assert submit(client, guest_headers, mood, choice_code="EXCITED").status_code == 200
    today.value += timedelta(days=7)
    use_ai(app, lambda request: ai_response("USAGE_DAYS", "표시된 기간 동안 운동화를 며칠 신었나요?"))
    first = generate(client, guest_headers, purchase)
    assert first["context"]["usage_window"] == {
        "start": purchase["purchased_on"],
        "end": str(today.value - timedelta(days=1)),
        "days": 7,
    }
    actual = submit(client, guest_headers, first, choice_code="D2").json()
    assert actual["comparison"]["expected_used_days"] == pytest.approx(14 / 3)
    assert actual["comparison"]["difference_percentage_points"] == 38.1
    # Repeated submissions and generation do not duplicate the weekly window.
    assert submit(client, guest_headers, first, choice_code="D3").status_code == 200
    today.value += timedelta(days=1)
    use_ai(app, lambda request: ai_response("USAGE_CONTEXT"))
    context_question = generate(client, guest_headers, purchase)
    assert context_question["template_code"] == "USAGE_CONTEXT"
    assert submit(client, guest_headers, context_question, choice_code="EXERCISE").status_code == 200
    today.value += timedelta(days=6)
    use_ai(app, lambda request: ai_response("USAGE_DAYS", "표시된 기간 동안 운동화를 며칠 사용했나요?"))
    second = generate(client, guest_headers, purchase)
    assert second["context"]["usage_window"]["start"] == str(
        date.fromisoformat(purchase["purchased_on"]) + timedelta(days=7)
    )
    assert submit(client, guest_headers, second, choice_code="D0").status_code == 200
    history = client.get(f"{P}/purchases/{purchase['id']}/questions", headers=guest_headers).json()
    assert history["usage_summary"]["reported_used_days"] == 3
    assert history["usage_summary"]["observed_days"] == 14
    assert history["usage_summary"]["cost_per_reported_used_day"] == 10000
    assert history["usage_summary"]["covers_entire_elapsed_period"] is True
    # New measurements feed reports; an old review must not count this purchase twice.
    assert (
        client.put(
            f"{P}/purchases/{purchase['id']}/review",
            headers=guest_headers,
            json={"frequency": "ALMOST_DAILY"},
        ).status_code
        == 200
    )
    category = client.get(P + "/reports?month=2026-11", headers=guest_headers).json()["categories"][0]
    assert category["sample_count"] == 1 and category["measurement_count"] == 2
    assert category["actual_days_estimate"] == 3 and category["observed_days"] == 14
    assert category["is_estimate"] is False
    today.value += timedelta(days=1)
    use_ai(app, lambda request: ai_response("NON_USE_REASON"))
    followup = generate(client, guest_headers, purchase)
    assert followup["template_code"] == "NON_USE_REASON"
    assert session.scalar(select(func.count(PurchaseQuestionAnswer.id))) == 4


def test_unknown_usage_is_missing_not_zero_and_no_nonuse_question(app, client, guest_headers, today):
    purchase = purchase_item(client, guest_headers, today)
    today.value += timedelta(days=7)
    use_ai(app, lambda request: ai_response("USAGE_DAYS"))
    question = generate(client, guest_headers, purchase)
    response = submit(client, guest_headers, question, choice_code="UNSURE").json()
    assert response["comparison"]["actual_used_days"] is None
    assert response["comparison"]["reason"] == "UNKNOWN_USAGE"
    today.value += timedelta(days=1)
    use_ai(app, lambda request: ai_response("NON_USE_REASON"))
    followup = generate(client, guest_headers, purchase)
    assert followup["template_code"] == "USAGE_CONTEXT" and followup["generation_method"] == "TEMPLATE"
    history = client.get(f"{P}/purchases/{purchase['id']}/questions", headers=guest_headers).json()
    assert history["usage_summary"]["observed_days"] == 0


def test_long_term_price_estimate_and_question_owner_protection(app, client, guest_headers, today):
    purchase = purchase_item(client, guest_headers, today)
    today.value = month_anniversary(today.value, 3)
    use_ai(app, lambda request: ai_response("RESALE_VALUE"))
    question = generate(client, guest_headers, purchase)
    assert question["stage"] == "LONG_TERM"
    response = submit(client, guest_headers, question, number_value=12000)
    assert response.status_code == 200 and response.json()["comparison"]["estimated_loss"] == 18000
    for body in (
        {"number_value": True},
        {"number_value": -1},
        {"number_value": 1.5},
        {"choice_code": "YES"},
        {"number_value": 3, "choice_code": "UNSURE"},
    ):
        assert submit(client, guest_headers, question, **body).status_code == 422
    other = client.post(P + "/guest-sessions").json()
    headers = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(f"{P}/purchase-questions/{question['id']}", headers=headers).status_code == 404
    assert submit(client, headers, question, number_value=1).status_code == 404
    assert client.get(f"{P}/purchases/{purchase['id']}/questions", headers=headers).status_code == 404
    assert client.post(f"{P}/purchases/{purchase['id']}/daily-question", headers=headers).status_code == 404


def test_concurrent_generation_freezes_single_winner(app, client, guest_headers, today, session):
    if session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent requests")
    purchase = purchase_item(client, guest_headers, today)
    barrier = Barrier(2)

    def handler(request):
        barrier.wait(timeout=10)
        return ai_response("EXPECTATION")

    use_ai(app, handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: generate(client, guest_headers, purchase), range(2)))
    assert results[0] == results[1]
    assert session.scalar(select(func.count(PurchaseQuestion.id))) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: submit(client, guest_headers, results[0], choice_code="SIMILAR"), range(2))
        )
    assert all(response.status_code == 200 for response in responses)
    assert session.scalar(select(func.count(PurchaseQuestionAnswer.id))) == 1


def test_ai_corrects_changed_usage_unit_once(app, client, guest_headers, today):
    purchase = purchase_item(client, guest_headers, today)
    today.value += timedelta(days=7)
    calls = []

    def handler(request):
        calls.append(request)
        return ai_response(
            "USAGE_DAYS",
            "이번 달에 운동화를 몇 번 썼나요?"
            if len(calls) == 1
            else "표시된 기간 동안 운동화를 며칠 신었나요?",
        )

    use_ai(app, handler)
    question = generate(client, guest_headers, purchase)
    assert len(calls) == 2 and question["generation_method"] == "AI"
    assert "(7일)" in question["reference_text"]
    assert question["context"]["usage_window"]["start"] in question["reference_text"]


def test_value_question_does_not_invent_lifetime_cost(app, client, guest_headers, today):
    purchase = purchase_item(client, guest_headers, today)
    today.value = month_anniversary(today.value, 1)
    use_ai(app, lambda request: ai_response("VALUE_WORTH"))
    question = generate(client, guest_headers, purchase)
    assert question["stage"] == "VALUE" and question["generation_method"] == "AI"
    assert "30,000원" in question["reference_text"]
    assert "계산하지 않았어요" in question["reference_text"]
    assert question["context"]["usage_summary"]["cost_per_reported_used_day"] is None
