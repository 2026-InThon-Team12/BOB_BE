import base64
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.core.config import Settings
from app.integrations.lablup import LablupError
from app.models import (
    Goal,
    Simulation,
    SimulationQuestion,
    SimulationQuestionAnswer,
    PurchaseQuestion,
    DailyAnswer,
)
from app.services.ai import AiService
from app.services.cart_discounts import CartDiscountRequest, calculate_cart
from app.services.local_questions import LocalSummaryRequest, summarize_local
from tests.test_finance import _goal, _impulse, _decide

P = "/api/v1"


def item_input(**fields):
    return {
        "name": "키보드",
        "price": 30000,
        "category": "DIGITAL",
        "purchase_reasons": ["NECESSITY"],
        **fields,
    }


def register(client, headers, item=None, key="handoff-register", **extra):
    return client.post(
        P + "/items/batch",
        headers={**headers, "Idempotency-Key": key},
        json={"items": [item or item_input()], **extra},
    )


def cart(items, **rule):
    return calculate_cart(
        CartDiscountRequest.model_validate(
            {"items": items, "rule": {"minimum_amount": 30000, "unit": "PCT", "value": 10, **rule}}
        )
    )


def test_toggle_checkout_uses_threshold_on_both_carts():
    lines = [
        {"client_id": "shirt", "name": "티셔츠", "price": 20000},
        {"client_id": "socks", "name": "양말", "price": 10000, "purchase_reasons": ["EXTRA_DISCOUNT"]},
    ]
    result = cart(lines)
    assert result["needed"]["total"] == 20000 and result["all"]["total"] == 27000
    assert result["extra_spending"] == 7000 and result["verdict"] == "TRAP"
    assert not result["needed"]["discount_eligible"] and result["all"]["discount_eligible"]
    lines[1]["enabled"] = True
    result = cart(lines)
    assert result["verdict"] == "SMART" and result["saved_amount"] == 3000
    for line in lines:
        line["enabled"] = False
    assert cart(lines, shipping_fee=3000)["needed"]["total"] == 0


def test_half_up_discount_cap_and_shipping_dont_invent_savings():
    lines = [{"client_id": "a", "name": "물건", "price": 101}]
    assert cart(lines, minimum_amount=0, value=50)["all"]["discount"] == 51
    assert cart(lines, minimum_amount=0, unit="WON", value=500)["all"]["total"] == 0
    result = cart(lines, minimum_amount=0, shipping_fee=100, free_shipping_minimum=110)
    assert result["pay_shipping_saves"] == 0  # reaching the threshold is cheaper here


def test_discount_gate_preview_and_filtered_registration(client, guest_headers, session):
    body = {
        "items": [
            item_input(),
            item_input(name="추가 물건", price=10000, purchase_reasons=["EXTRA_DISCOUNT"]),
        ]
    }
    h = {**guest_headers, "Idempotency-Key": "toggle-register"}
    assert client.post(P + "/items/batch", headers=h, json=body).status_code == 409
    preview = client.post(P + "/registration-previews", headers=guest_headers, json=body).json()
    assert preview["discount_check_required"]
    body["discount_review"] = {
        "rule": {"minimum_amount": 40000, "unit": "PCT", "value": 10},
        "needed_item_indexes": [0],
        "decision": "NEEDED",
    }
    created = client.post(P + "/items/batch", headers=h, json=body)
    assert created.status_code == 201, created.text
    assert len(created.json()["items"]) == 1
    assert created.json()["discount"]["needed"]["discount"] == 0
    assert client.post(P + "/items/batch", headers=h, json=body).json() == created.json()
    assert session.scalar(select(func.count(Simulation.id))) == 1
    assert client.post(P + "/discount-extractions", headers=guest_headers).status_code == 410


@pytest.mark.parametrize(
    "gap,days,status", [(0, 0, "COMPLETED"), (1, 1, "SCHEDULED"), (2, 2, "SCHEDULED"), (20, 14, "SCHEDULED")]
)
def test_sale_deadline_shortens_and_today_goes_to_result(client, guest_headers, today, gap, days, status):
    value = item_input(
        price=189000,
        purchase_reasons=["DISCOUNT_LIMITED", "OTHER"],
        reason_other_text="출근 때 쓰려고",
        sale_ends_on=(today.value + timedelta(days=gap)).isoformat(),
    )
    response = register(client, guest_headers, value)
    assert response.status_code == 201, response.text
    item = response.json()["items"][0]
    assert (
        item["sale_ends_on"] == value["sale_ends_on"]
        and item["reason_other_text"] == value["reason_other_text"]
    )
    assert item["simulation"]["duration_days"] == days and item["simulation"]["status"] == status
    if gap == 0:
        result = client.get(
            f"{P}/simulations/{item['simulation']['id']}/result", headers=guest_headers
        ).json()
        assert result["comparison_reason"] == "NO_OBSERVATION_PERIOD" and result["answer_coverage"] == 0
        assert (
            client.post(
                f"{P}/simulations/{item['simulation']['id']}/local-question", headers=guest_headers, json={}
            ).status_code
            == 409
        )
        assert (
            client.put(
                f"{P}/items/{item['id']}/decision",
                headers={**guest_headers, "Idempotency-Key": "hold-no-days"},
                json={"decision": "HOLD"},
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"purchase_reasons": ["OTHER"]},
        {"reason_other_text": "없는 기타 이유"},
        {"purchase_reasons": ["DISCOUNT_LIMITED"]},
        {"purchase_reasons": ["DISCOUNT_LIMITED"], "sale_ends_on": "2026-10-30"},
        {"purchase_reasons": ["OTHER"], "reason_other_text": "  "},
    ],
)
def test_reason_details_are_required_and_consistent(client, guest_headers, fields):
    assert register(client, guest_headers, item_input(**fields)).status_code == 422


def test_local_questions_summary_and_purchase_do_not_persist_transcript(
    client, guest_headers, today, session
):
    item = register(client, guest_headers).json()["items"][0]
    sid = item["simulation"]["id"]
    assert item["simulation"]["question_mode"] == "LOCAL"
    start = today.value + timedelta(days=1)
    today.value = start
    question = client.post(f"{P}/simulations/{sid}/local-question", headers=guest_headers, json={}).json()
    assert question["storage"] == "LOCAL_ONLY"
    assert (
        client.put(
            f"{P}/simulations/{sid}/answers/{today.value}",
            headers=guest_headers,
            json={"usage_answer": "YES"},
        ).status_code
        == 409
    )
    tasks = client.get(P + "/today", headers=guest_headers).json()["pre_purchase_questions"]
    assert tasks[0]["answer_state"] == "CLIENT_MANAGED"
    records = [
        {
            "date": start.isoformat(),
            "template_code": "DISCOVERY",
            "question": "어떻게 알았나요?",
            "answer": {"choice_code": "ADVERTISEMENT"},
        },
        {
            "date": (start + timedelta(days=1)).isoformat(),
            "template_code": "PROBLEM_TO_SOLVE",
            "question": "어떤 문제인가요?",
            "answer": {"text_value": "PRIVATE_LOCAL_TEXT"},
        },
        {
            "date": (start + timedelta(days=2)).isoformat(),
            "template_code": "EXPECTED_USE_DAYS",
            "question": "30일 중 며칠인가요?",
            "answer": {"number_value": 12},
        },
    ]
    endpoint = f"{P}/simulations/{sid}/result-summary"
    assert client.post(endpoint, headers=guest_headers, json={"responses": records}).status_code == 409
    today.value = start + timedelta(days=3)
    summary = client.post(endpoint, headers=guest_headers, json={"responses": records})
    assert summary.status_code == 200, summary.text
    result = summary.json()["result"]
    assert result["answered_days"] == 3 and result["expected_used_days"] == 12
    assert result["expected_usage_rate"] == 0.4 and result["comparison_eligible"]
    assert summary.json()["ai_summary"]["method"] == "TEMPLATE"
    assert not summary.json()["raw_responses_stored"]
    assert "PRIVATE_LOCAL_TEXT" not in json.dumps(session.get(Simulation, sid).local_result)
    for model in (SimulationQuestion, SimulationQuestionAnswer, DailyAnswer, PurchaseQuestion):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    bought = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    assert client.post(endpoint, headers=guest_headers, json={"responses": records}).status_code == 409
    generated = client.post(f"{P}/purchases/{bought['id']}/local-question", headers=guest_headers, json={})
    assert generated.status_code == 200 and generated.json()["storage"] == "LOCAL_ONLY"
    assert session.scalar(select(func.count(PurchaseQuestion.id))) == 0
    assert (
        client.post(f"{P}/purchases/{bought['id']}/daily-question", headers=guest_headers).status_code == 409
    )


def test_local_summary_validates_dates_semantics_and_ownership(client, guest_headers, today):
    item = register(client, guest_headers).json()["items"][0]
    sid = item["simulation"]["id"]
    today.value += timedelta(days=4)
    other = client.post(P + "/guest-sessions").json()
    h = {"Authorization": "Bearer " + other["access_token"]}
    url = f"{P}/simulations/{sid}/result-summary"
    assert client.post(url, headers=h, json={}).status_code == 404
    row = {
        "date": "2026-11-01",
        "template_code": "CURRENT_MOOD",
        "question": "기분은?",
        "answer": {"number_value": 3},
    }
    assert client.post(url, headers=guest_headers, json={"responses": [row]}).status_code == 422
    row.update(template_code="EXPECTED_USE_DAYS", date="2026-11-05")
    assert client.post(url, headers=guest_headers, json={"responses": [row]}).status_code == 422
    row["date"] = "2026-11-01"
    assert client.post(url, headers=guest_headers, json={"responses": [row, row]}).status_code == 422


def test_post_local_non_use_question_uses_review_aggregate(client, guest_headers, today):
    item = register(client, guest_headers).json()["items"][0]
    purchase = client.put(
        f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "BUY"}
    ).json()["purchase"]
    today.value += timedelta(days=14)
    assert (
        client.put(
            f"{P}/purchases/{purchase['id']}/review", headers=guest_headers, json={"used_days": 0}
        ).status_code
        == 200
    )
    question = client.post(f"{P}/purchases/{purchase['id']}/local-question", headers=guest_headers, json={})
    assert question.status_code == 200
    assert question.json()["template_code"] == "NON_USE_REASON"


def test_summary_model_repair_timeout_and_fallback():
    request = LocalSummaryRequest.model_validate(
        {
            "responses": [
                {
                    "date": "2026-11-01",
                    "template_code": "EXPECTED_USE_DAYS",
                    "question": "며칠?",
                    "answer": {"number_value": 5},
                }
            ]
        }
    )

    class Fake:
        def __init__(self, values):
            self.values, self.calls = iter(values), 0

        def message(self, **kwargs):
            self.calls += 1
            result = next(self.values)
            if isinstance(result, Exception):
                raise result
            return result

    ai = Fake(["{}", '{"summary":"5일 쓸 것으로 예상했어요.","source_dates":["2026-11-01"]}'])
    config = Settings(_env_file=None)
    result = {"response_summary": "fallback"}
    assert summarize_local(request, result, config, ai)["method"] == "AI" and ai.calls == 2
    for outputs in ([LablupError("AI_TIMEOUT")], ["{}", "{}"]):
        assert summarize_local(request, result, config, Fake(outputs))["summary"] == "fallback"


def test_vision_receives_isolated_photos_and_preserves_server_prices():
    from PIL import ImageDraw
    from test_cart_layout import cart_fixture, descriptions

    image, lines = cart_fixture()
    ImageDraw.Draw(image).rectangle((140, 420, 340, 620), fill="red")
    ImageDraw.Draw(image).rectangle((140, 1030, 340, 1230), fill="blue")
    data = io.BytesIO()
    image.save(data, format="PNG")
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right - line.left}\t{line.bottom - line.top}\t95\t{line.text}"
        for line in lines
    )

    class Fake:
        calls = []

        def message(self, **kwargs):
            self.calls.append(kwargs)
            return json.dumps(descriptions())

    config = Settings(_env_file=None, lablup_vision_enabled=True)
    fake = Fake()
    ai = AiService(config, client=fake, ocr=lambda *_: table, price_reader=lambda *_: ["27,500", "20,500"])
    result = ai.extract_cart(data.getvalue())
    assert result.analysis_method == "vision"
    assert [item.price for item in result.items] == [27500, 20500]
    pictures = [b for b in fake.calls[0]["content"] if b["type"] == "image"]
    assert len(pictures) == 2
    for index, picture in enumerate(pictures):
        with Image.open(io.BytesIO(base64.b64decode(picture["source"]["data"]))) as crop:
            assert max(crop.size) <= 320
            assert crop.getpixel((crop.width // 2, crop.height // 2))[index * 2] > 240
    config.lablup_vision_enabled = False
    result = ai.extract_cart(data.getvalue())
    assert result.analysis_method == "ocr"
    assert all(b["type"] == "text" for b in fake.calls[1]["content"])
    assert all(item.image_crop for item in result.items)


def test_shelf_displays_on_achievement_without_purchase_and_replays_once(
    client, guest_headers, today, session
):
    goal = _goal(client, guest_headers, target=10000)
    row = session.get(Goal, goal["id"])
    row.created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    session.commit()
    url = f"{P}/goals/{goal['id']}/purchase"
    body = {"purchased_on": today.value.isoformat(), "actual_price": 9000}
    assert client.put(url, headers=guest_headers, json=body).status_code == 410
    assert client.get(P + "/goals/shelf", headers=guest_headers).json()["count"] == 0
    impulse = _impulse(client, guest_headers, amount=10000)
    assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 200
    assert _decide(client, guest_headers, impulse, "RESISTED").status_code == 200
    shelf = client.get(P + "/goals/shelf", headers=guest_headers).json()
    assert shelf["count"] == 1 and shelf["total_target_amount"] == 10000
    assert shelf["items"][0]["achieved_on"] == today.value.isoformat()
    assert "total_purchase_amount" not in shelf and "review" not in shelf["items"][0]
    assert shelf["items"][0]["resisted_count"] == 1
    assert client.get(P + "/goals/current", headers=guest_headers).json()["shelf_count"] == 1
    assert (
        client.patch(P + "/goals/current", headers=guest_headers, json={"name": "다른 목표"}).status_code
        == 409
    )
    assert _decide(client, guest_headers, impulse, "CANCELLED").status_code == 200
    assert client.get(P + "/goals/shelf", headers=guest_headers).json() == shelf
    session.expire_all()
    assert session.get(Goal, goal["id"]).purchased_on is None
