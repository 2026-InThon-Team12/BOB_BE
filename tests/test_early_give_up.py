from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.core.clock import SEOUL
from app.core.config import Settings
from app.models import (
    DailyAnswer,
    Expense,
    Purchase,
    PurchaseReview,
    SavingEvent,
    Simulation,
    SimulationQuestion,
    SimulationQuestionAnswer,
)
from app.workers.notifications import run_once
from tests.test_notifications import FakeSender
from tests.test_pre_purchase_questions import generate, submit, use_ai

P = "/api/v1"
START = date(2026, 10, 31)


def register(client, headers, mode="LOCAL"):
    response = client.post(
        P + "/items/batch",
        headers={**headers, "Idempotency-Key": "early-give-up-register"},
        json={
            "items": [
                {
                    "name": "키보드",
                    "price": 20000,
                    "category": "DIGITAL",
                    "purchase_reasons": ["NECESSITY"],
                    "question_mode": mode,
                }
            ]
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["items"][0]


def decide(client, headers, item, decision, key="early-give-up-decision"):
    return client.put(
        f"{P}/items/{item['id']}/decision",
        headers={**headers, "Idempotency-Key": key},
        json={"decision": decision},
    )


@pytest.mark.parametrize("mode", ["LOCAL", "ADAPTIVE", "LEGACY"])
@pytest.mark.parametrize("offset,extend", [(0, False), (1, False), (2, False), (6, True), (4, False)])
def test_give_up_lifecycle(client, guest_headers, today, session_factory, mode, offset, extend):
    client.post(P + "/goals", headers=guest_headers, json={"name": "목표", "target_amount": 20000})
    item = register(client, guest_headers, mode)
    sid = item["simulation"]["id"]
    if extend:
        today.value = START + timedelta(days=4)
        assert decide(client, guest_headers, item, "HOLD", "hold-original-key").status_code == 200
    today.value = START + timedelta(days=offset)
    early = extend or offset < 4
    if early:
        assert decide(client, guest_headers, item, "HOLD", "hold-new-key").status_code == 409
    response = decide(client, guest_headers, item, "GIVE_UP")
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert confirmed["decision"] == "GIVE_UP" and confirmed["decided_on"] == today.value.isoformat()
    assert confirmed["purchase"] is None
    assert confirmed["simulation"]["status"] == ("EARLY_GIVE_UP" if early else "COMPLETED")
    assert decide(client, guest_headers, item, "GIVE_UP").json() == confirmed
    assert decide(client, guest_headers, item, "BUY").status_code == 409
    assert decide(client, guest_headers, item, "HOLD", "hold-original-key").status_code == 409
    assert (
        client.patch(f"{P}/items/{item['id']}", headers=guest_headers, json={"price": 100}).status_code == 409
    )
    initial = client.get(f"{P}/simulations/{sid}/result", headers=guest_headers).json()
    assert initial["pending_days"] == 0
    if early:
        assert initial["comparison_reason"] == "EARLY_GIVE_UP" and not initial["comparison_eligible"]
        assert initial["observation_end_date_exclusive"] == (today.value + timedelta(days=1)).isoformat()
        if mode == "LOCAL":
            assert initial["answer_state"] == "CLIENT_MANAGED"
            assert initial["answered_days"] is None and initial["unanswered_days"] is None
    report = client.get(P + "/reports", headers=guest_headers).json()
    assert report["bye_count"] == 1 and report["saved_amount"] == 20000
    reason = next(r for r in report["purchase_reasons"] if r["reason"] == "NECESSITY")
    assert reason["abandoned_count"] == 1 and reason["abandonment_rate"] == 1
    assert not report["categories"]
    assert len(client.get(P + "/goals/shelf", headers=guest_headers).json()["items"]) == 1
    for days in (0, 1, 30):
        today.value = START + timedelta(days=offset + days)
        assert not client.get(P + "/home", headers=guest_headers).json()["simulations"]
        cards = client.get(P + "/today?include_answered=true", headers=guest_headers).json()
        assert all(
            not cards[key]
            for key in ("questions", "decisions", "reviews", "pre_purchase_questions", "purchase_questions")
        )
        stored = client.get(f"{P}/items/{item['id']}", headers=guest_headers).json()
        assert stored == confirmed
        records = client.get(P + "/items", headers=guest_headers).json()["items"]
        assert [r["id"] for r in records] == [item["id"]]
        result = client.get(f"{P}/simulations/{sid}/result", headers=guest_headers).json()
        for field in ("observed_days", "unanswered_days", "pending_days", "status", "comparison_reason"):
            assert result[field] == initial[field]
        for suffix, body in (("local-question", {"recent_templates": []}), ("daily-question", None)):
            assert (
                client.post(f"{P}/simulations/{sid}/{suffix}", headers=guest_headers, json=body).status_code
                == 409
            )
        assert (
            client.put(
                f"{P}/simulations/{sid}/answers/{today.value}",
                headers=guest_headers,
                json={"usage_answer": "YES"},
            ).status_code
            == 409
        )
    with session_factory() as session:
        assert session.scalar(select(func.count(SavingEvent.id))) == 1
        assert session.scalar(select(func.sum(SavingEvent.amount))) == 20000
        for model in (Expense, Purchase, PurchaseReview):
            assert session.scalar(select(func.count(model.id))) == 0
        assert session.get(Simulation, sid).ended_early_on is None
    client.put(P + "/me/push-token", headers=guest_headers, json={"token": "early-give-up-fake-token"})
    sender = FakeSender()
    outcome = run_once(
        session_factory,
        sender,
        now=datetime.combine(today.value, datetime.min.time(), tzinfo=SEOUL).replace(hour=20),
        settings=Settings(_env_file=None),
    )
    assert outcome.sent == 0 and not sender.calls
    other = {"Authorization": "Bearer " + client.post(P + "/guest-sessions").json()["access_token"]}
    assert decide(client, other, item, "GIVE_UP").status_code == 404


@pytest.mark.parametrize("mode", ["LEGACY", "ADAPTIVE"])
def test_give_up_retains_existing_question_and_answer(
    client, guest_headers, today, session_factory, app, mode
):
    item = register(client, guest_headers, mode)
    today.value += timedelta(days=1)
    if mode == "ADAPTIVE":
        use_ai(app, "CURRENT_MOOD")
        question = generate(client, guest_headers, item)
        assert submit(client, guest_headers, question, choice_code="STRESS").status_code == 200
    else:
        assert (
            client.put(
                f"{P}/simulations/{item['simulation']['id']}/answers/{today.value}",
                headers=guest_headers,
                json={"usage_answer": "YES"},
            ).status_code
            == 200
        )
    assert decide(client, guest_headers, item, "GIVE_UP").status_code == 200
    today.value += timedelta(days=40)
    result = client.get(f"{P}/simulations/{item['simulation']['id']}/result", headers=guest_headers).json()
    assert result["answered_days"] == 1 and result["unanswered_days"] == 0
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count(DailyAnswer.id if mode == "LEGACY" else SimulationQuestionAnswer.id))
            )
            == 1
        )
        if mode == "ADAPTIVE":
            assert session.scalar(select(func.count(SimulationQuestion.id))) == 1


def test_concurrent_early_give_up_credits_once(client, guest_headers, today, session_factory):
    if session_factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    item = register(client, guest_headers)
    barrier = Barrier(2)

    def request(_):
        barrier.wait(timeout=10)
        return decide(client, guest_headers, item, "GIVE_UP")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json() == results[1].json()
    with session_factory() as session:
        assert session.scalar(select(func.count(SavingEvent.id))) == 1


def test_non_buy_purchase_fields_are_still_rejected(client, guest_headers):
    item = register(client, guest_headers)
    for fields in ({"actual_price": 100}, {"purchased_on": START.isoformat()}):
        response = client.put(
            f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "GIVE_UP", **fields}
        )
        assert response.status_code == 422
