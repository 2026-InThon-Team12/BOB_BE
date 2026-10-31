from sqlalchemy import func, select

from app.models import Goal, SavingEvent
from tests.test_core import create_item
from tests.test_finance import _goal, _impulse, _decide

P = "/api/v1"


def test_first_setup_candidates_and_preview_are_owner_scoped_read_only(client, guest_headers, session):
    create_item(client, guest_headers)
    setup = client.get(P + "/goals/setup", headers=guest_headers).json()
    assert setup["mode"] == "FIRST" and setup["current_goal"] is None
    assert len(setup["candidates"]) == 1 and setup["candidates"][0]["target_amount"] == 30000
    result = client.post(
        P + "/goals/preview",
        headers=guest_headers,
        json={"mode": "FIRST", "name": "목표", "target_amount": 10000},
    ).json()
    assert result["remaining_amount"] == 10000 and result["starting_saved_amount"] == 0
    assert session.scalar(select(func.count(Goal.id))) == 0
    other = client.post(P + "/guest-sessions").json()
    h = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(P + "/goals/setup", headers=h).json()["candidates"] == []


def test_change_preview_matches_on_off_cap_and_does_not_touch_ledger(client, guest_headers, session):
    goal = _goal(client, guest_headers, target=240000)
    impulse = _impulse(client, guest_headers, amount=151000)
    _decide(client, guest_headers, impulse, "RESISTED")
    assert client.get(P + "/goals/setup", headers=guest_headers).json()["mode"] == "CHANGE"
    body = {"mode": "CHANGE", "current_goal_id": goal["id"], "name": "새 목표", "target_amount": 200000}
    response = client.post(P + "/goals/preview", headers=guest_headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["starting_saved_amount"] == 151000 and response.json()["remaining_amount"] == 49000
    body["target_amount"] = 100000
    result = client.post(P + "/goals/preview", headers=guest_headers, json=body).json()
    assert result["starting_saved_amount"] == 100000 and result["achieved"]
    body["carry_savings"] = False
    assert (
        client.post(P + "/goals/preview", headers=guest_headers, json=body).json()["starting_saved_amount"]
        == 0
    )
    assert session.scalar(select(func.count(SavingEvent.id))) == 1
    assert session.scalar(select(func.count(Goal.id))) == 1
    assert client.get(P + "/goals/current", headers=guest_headers).json()["saved_amount"] == 151000
    body["current_goal_id"] = "stale"
    assert client.post(P + "/goals/preview", headers=guest_headers, json=body).status_code == 409


def test_next_preview_requires_achievement_and_does_not_reuse_achieved_money(client, guest_headers):
    goal = _goal(client, guest_headers, target=10000)
    body = {"mode": "NEXT", "current_goal_id": goal["id"], "name": "다음 목표", "target_amount": 10000}
    assert client.post(P + "/goals/preview", headers=guest_headers, json=body).status_code == 409
    impulse = _impulse(client, guest_headers, amount=15000)
    _decide(client, guest_headers, impulse, "RESISTED")
    assert client.get(P + "/goals/setup", headers=guest_headers).json()["mode"] == "NEXT"
    assert (
        client.post(P + "/goals/preview", headers=guest_headers, json=body).json()["starting_saved_amount"]
        == 0
    )
    body["mode"] = "CHANGE"
    assert client.post(P + "/goals/preview", headers=guest_headers, json=body).status_code == 409
