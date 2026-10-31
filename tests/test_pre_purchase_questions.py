import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select

from app.api.pre_purchase import get_pre_question_ai
from app.core.config import Settings
from app.integrations.lablup import LablupClient
from app.models import SimulationQuestion, SimulationQuestionAnswer
from app.services.pre_purchase_questions import PreQuestionAI
from app.services import swipe_questions as swipe
from tests.test_core import create_item
from tests.test_handoff import item_input, register

P = "/api/v1"


def response_for(code, wording):
    return httpx.Response(
        200,
        json={
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "template_code": code,
                            "question_text": wording,
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
        payload = json.loads(request.content)
        calls.append(payload)
        if handler:
            return handler(request)
        selected = json.loads(payload["messages"][0]["content"][0]["text"])["selected_template"]
        return response_for(code or selected["template_code"], selected["question"])

    ai = PreQuestionAI(config, LablupClient(config, httpx.Client(transport=httpx.MockTransport(wrapped))))
    app.dependency_overrides[get_pre_question_ai] = lambda: ai
    return calls


def generate(client, headers, item):
    response = client.post(f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, headers, question, **body):
    return client.put(f"{P}/simulation-questions/{question['id']}/answer", headers=headers, json=body)


def assert_swipe(question):
    assert question["template_version"] == swipe.VERSION
    assert question["answer_spec"] == swipe.ANSWER_SPEC
    assert [o["code"] for o in question["answer_spec"]["options"]] == ["YES", "UNSURE", "NO"]
    assert question["question"].endswith("?")


def test_catalog_is_the_eight_requested_intents_plus_custom(client):
    catalog = client.get(P + "/pre-purchase-question-templates").json()
    assert catalog["version"] == swipe.VERSION
    assert [r["code"] for r in catalog["templates"]] == list(swipe.PRE_BY_CODE)
    assert len(catalog["templates"]) == 9
    assert all(row["answer_spec"] == swipe.ANSWER_SPEC for row in catalog["templates"])


def test_adaptive_generation_replay_answer_changes_and_ownership(app, client, guest_headers, today, session):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    calls = use_ai(app)
    assert (
        client.post(
            f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=guest_headers
        ).status_code
        == 409
    )
    today.value += timedelta(days=1)
    assert client.get(P + "/today", headers=guest_headers).status_code == 200
    assert not calls
    q = generate(client, guest_headers, item)
    assert_swipe(q)
    assert q["generation_method"] == "AI"
    assert generate(client, guest_headers, item) == q and len(calls) == 1
    for code in ("YES", "UNSURE", "NO"):
        assert submit(client, guest_headers, q, choice_code=code).status_code == 200
    for body in ({"number_value": 5}, {"text_value": "집"}, {"choice_code": "HOME"}):
        assert submit(client, guest_headers, q, **body).status_code == 422
    assert session.scalar(select(func.count(SimulationQuestionAnswer.id))) == 1
    other = client.post(P + "/guest-sessions").json()
    foreign = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(f"{P}/simulation-questions/{q['id']}", headers=foreign).status_code == 404
    assert submit(client, foreign, q, choice_code="YES").status_code == 404
    today.value += timedelta(days=1)
    assert submit(client, guest_headers, q, choice_code="YES").status_code == 409


def test_local_all_days_are_swipes_with_rotation_and_no_forced_number(client, guest_headers, today, session):
    item = register(
        client, guest_headers, item_input(name="요가매트", category="HOBBY", price=150000)
    ).json()["items"][0]
    history = []
    for day in range(14):
        today.value += timedelta(days=1)
        response = client.post(
            f"{P}/simulations/{item['simulation']['id']}/local-question",
            headers=guest_headers,
            json={"recent_templates": history},
        )
        assert response.status_code == 200, response.text
        q = response.json()
        assert_swipe(q)
        assert q["template_code"] not in {r["template_code"] for r in history[-3:]}
        assert q["template_code"] != "SW_PRE_PROMO"
        assert "요가매트" in q["question"]
        history.append({"date": q["question_date"], "template_code": q["template_code"]})
    assert len({r["template_code"] for r in history[:8]}) == 8
    assert session.scalar(select(func.count(SimulationQuestion.id))) == 0


def test_local_context_includes_recorded_reason_but_not_daily_answers(app, client, guest_headers, today):
    item = register(
        client,
        guest_headers,
        item_input(
            name="요가매트",
            category="HOBBY",
            purchase_reasons=["OTHER"],
            reason_other_text="매일 스트레칭할 계획",
        ),
    ).json()["items"][0]
    calls = use_ai(app)
    today.value += timedelta(days=1)
    q = client.post(
        f"{P}/simulations/{item['simulation']['id']}/local-question", headers=guest_headers, json={}
    ).json()
    assert_swipe(q)
    context = json.loads(calls[0]["messages"][0]["content"][0]["text"])["context"]
    assert context["reason_other_text"] == "매일 스트레칭할 계획"
    assert "previous_answers" not in context


def test_cash_question_displays_the_registered_price(client, guest_headers, today, monkeypatch):
    monkeypatch.setattr(
        swipe.secrets, "choice", lambda pool: next(t for t in pool if t.code == "SW_PRE_CASH")
    )
    item = register(client, guest_headers).json()["items"][0]
    today.value += timedelta(days=1)
    q = client.post(
        f"{P}/simulations/{item['simulation']['id']}/local-question", headers=guest_headers, json={}
    ).json()
    assert q["reference_text"] == "등록 가격 30,000원"
    assert_swipe(q)


def test_local_summary_counts_yes_as_forecast_days_in_original_window(client, guest_headers, today):
    item = register(client, guest_headers).json()["items"][0]
    sid, rows = item["simulation"]["id"], []
    for choice in ("YES", "UNSURE", "NO"):
        today.value += timedelta(days=1)
        q = client.post(
            f"{P}/simulations/{sid}/local-question",
            headers=guest_headers,
            json={
                "recent_templates": [{"date": r["date"], "template_code": r["template_code"]} for r in rows]
            },
        ).json()
        rows.append(
            {
                "date": q["question_date"],
                "template_code": q["template_code"],
                "question": q["question"],
                "answer": {"choice_code": choice},
            }
        )
    today.value += timedelta(days=1)
    response = client.post(
        f"{P}/simulations/{sid}/result-summary", headers=guest_headers, json={"responses": rows}
    )
    assert response.status_code == 200, response.text
    r = response.json()["result"]
    assert r["reflection_counts"] == {"YES": 1, "UNSURE": 1, "NO": 1}
    assert r["purchase_support_rate"] == pytest.approx(1 / 3)
    assert r["expected_used_days"] == 1 and r["expected_usage_rate"] == pytest.approx(1 / 3)
    assert r["comparison_eligible"] and r["metric_unit"] == "usage_days"
    assert r["prediction_window_days"] == 3
    fetched = client.get(f"{P}/simulations/{sid}/result", headers=guest_headers).json()
    assert fetched["reflection_counts"] == r["reflection_counts"]
    rows[0]["answer"] = {"number_value": 3}
    assert (
        client.post(
            f"{P}/simulations/{sid}/result-summary", headers=guest_headers, json={"responses": rows}
        ).status_code
        == 422
    )
    purchase = client.put(f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision":"BUY"}).json()["purchase"]
    assert purchase["expected_days_in_review_window"] == 1
    assert purchase["prediction_window_days"] == 3
    today.value += timedelta(days=14)
    assert client.put(f"{P}/purchases/{purchase['id']}/review", headers=guest_headers, json={"frequency":"DAYS_4_7"}).status_code == 200
    category = client.get(P + "/reports", headers=guest_headers).json()["categories"][0]
    assert category["predicted_days"] == 1
    assert category["prediction_window_days"] == 3
    assert category["actual_days_estimate"] == 5.5
    assert category["overprediction_ratio_estimate"] is None


def test_adaptive_summary_does_not_require_a_numeric_forecast(client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    for choice in ("YES", "NO", "UNSURE"):
        today.value += timedelta(days=1)
        q = generate(client, guest_headers, item)
        assert submit(client, guest_headers, q, choice_code=choice).status_code == 200
    today.value += timedelta(days=1)
    r = client.get(f"{P}/simulations/{item['simulation']['id']}/result", headers=guest_headers).json()
    assert r["reflection_counts"] == {"YES": 1, "UNSURE": 1, "NO": 1}
    assert r["comparison_reason"] is None
    assert r["expected_used_days"] == 1
    assert r["prediction_window_days"] == 3
    assert r["expected_usage_rate"] == pytest.approx(1 / 3)


def test_purchase_during_generation_blocks_late_question(app, client, guest_headers, today):
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)

    def handler(request):
        assert (
            client.put(
                f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
            ).status_code
            == 200
        )
        selected = json.loads(json.loads(request.content)["messages"][0]["content"][0]["text"])[
            "selected_template"
        ]
        return response_for(selected["template_code"], selected["question"])

    use_ai(app, handler=handler)
    assert (
        client.post(
            f"{P}/simulations/{item['simulation']['id']}/daily-question", headers=guest_headers
        ).status_code
        == 409
    )


def test_concurrent_generation_freezes_one_question(app, client, guest_headers, today, session):
    if session.bind.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL")
    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)
    barrier = Barrier(2)

    def handler(request):
        barrier.wait(timeout=10)
        selected = json.loads(json.loads(request.content)["messages"][0]["content"][0]["text"])[
            "selected_template"
        ]
        return response_for(selected["template_code"], selected["question"])

    use_ai(app, handler=handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: generate(client, guest_headers, item), range(2)))
    assert results[0] == results[1]
    assert session.scalar(select(func.count(SimulationQuestion.id))) == 1
