from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app.models import Expense, Purchase
from tests.test_core import create_item
from tests.test_finance import _accrue, _decide, _goal, _impulse
from tests.test_migration_upgrade import migration, migration_connection  # noqa: F401

P = "/api/v1"


def test_product_url_is_returned_without_recording_a_purchase(client, guest_headers, session, today):
    url = "https://shop.example.com/products/figure?color=blue"
    goal = client.post(
        P + "/goals",
        headers=guest_headers,
        json={"name": "피규어", "target_amount": 10000, "source_url": url},
    ).json()
    assert goal["source_url"] == url and goal["can_shop"]
    assert client.get(P + "/goals/current", headers=guest_headers).json()["source_url"] == url
    assert session.scalar(select(func.count(Purchase.id))) == 0
    assert session.scalar(select(func.count(Expense.id))) == 0
    impulse = _impulse(client, guest_headers, amount=10000)
    _decide(client, guest_headers, impulse, "RESISTED")
    shelf = client.get(P + "/goals/shelf", headers=guest_headers).json()
    assert shelf["items"] == []
    _accrue(client, guest_headers, today, 10000)
    shelf = client.get(P + "/goals/shelf", headers=guest_headers).json()
    assert shelf["items"][0]["source_url"] == url
    assert (
        client.patch(
            P + "/goals/current", headers=guest_headers, json={"source_url": "https://shop.example.com/new"}
        ).status_code
        == 200
    )
    assert (
        client.patch(P + "/goals/current", headers=guest_headers, json={"source_url": None}).json()[
            "can_shop"
        ]
        is False
    )
    other = client.post(P + "/guest-sessions").json()
    h = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get(P + "/goals/shelf", headers=h).json()["items"] == []


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://",
        "https://user:pass@shop.example.com",
        "https://shop.example.com:wrong",
        "https://shop.example.com/a b",
    ],
)
def test_invalid_goal_urls_are_rejected(client, guest_headers, url):
    assert (
        client.post(
            P + "/goals",
            headers=guest_headers,
            json={"name": "목표", "target_amount": 10000, "source_url": url},
        ).status_code
        == 422
    )


def test_unallocated_credit_and_lowering_target_create_achievements(client, guest_headers, today):
    impulse = _impulse(client, guest_headers, amount=10000)
    _decide(client, guest_headers, impulse, "RESISTED")
    goal = _goal(client, guest_headers, target=10000)
    assert goal["achieved_on"] is None
    goal = _accrue(client, guest_headers, today, 10000)
    assert goal["achieved_on"] == (today.value - timedelta(days=1)).isoformat() and goal["shelf_count"] == 1
    next_goal = client.post(
        P + "/goals/current/next",
        headers=guest_headers,
        json={"name": "다음 목표", "target_amount": 20000, "source_url": "https://shop.example.com/next"},
    ).json()
    assert next_goal["source_url"].endswith("/next") and next_goal["achieved_on"] is None
    impulse2 = _impulse(client, guest_headers, key="second-goal-credit", amount=10000)
    _decide(client, guest_headers, impulse2, "RESISTED")
    _accrue(client, guest_headers, today, 10000)
    patched = client.patch(P + "/goals/current", headers=guest_headers, json={"target_amount": 10000}).json()
    assert patched["achieved_on"] == (today.value - timedelta(days=1)).isoformat()
    assert client.get(P + "/goals/shelf", headers=guest_headers).json()["count"] == 2


def test_item_bye_preserves_history_without_funding_goal(client, guest_headers, today, session):
    _goal(client, guest_headers, target=10000)
    item = create_item(client, guest_headers)
    today.value += timedelta(days=4)
    assert (
        client.put(
            f"{P}/items/{item['id']}/decision", headers=guest_headers, json={"decision": "GIVE_UP"}
        ).status_code
        == 200
    )
    shelf = client.get(P + "/goals/shelf", headers=guest_headers).json()
    assert shelf["count"] == 0
    assert session.scalar(select(func.count(Purchase.id))) == 0


@pytest.mark.parametrize("enabled,needed_shipping,all_shipping", [(False, 3000, 3000), (True, 3000, 0)])
def test_shipping_switch_checks_each_cart_independently(
    client, guest_headers, enabled, needed_shipping, all_shipping
):
    body = {
        "items": [
            {"client_id": "a", "name": "a", "price": 20000, "enabled": True},
            {"client_id": "b", "name": "b", "price": 10000, "enabled": False},
        ],
        "rule": {
            "minimum_amount": 30000,
            "unit": "PCT",
            "value": 10,
            "free_shipping_enabled": enabled,
            "shipping_fee": 3000,
            "free_shipping_minimum": 30000,
        },
    }
    response = client.post(P + "/discount-previews", headers=guest_headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["needed"]["shipping"] == needed_shipping and result["all"]["shipping"] == all_shipping
    assert result["needed"]["total"] == 20000 + needed_shipping
    assert result["all"]["total"] == 27000 + all_shipping
    assert result["remaining_to_free_shipping"] == (0 if enabled else None)
    for item in body["items"]:
        item["enabled"] = False
    assert (
        client.post(P + "/discount-previews", headers=guest_headers, json=body).json()["needed"]["total"] == 0
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"free_shipping_enabled": False},
        {"free_shipping_enabled": True, "shipping_fee": 3000},
        {"free_shipping_enabled": True, "free_shipping_minimum": 30000},
    ],
)
def test_shipping_switch_requires_real_input(client, guest_headers, fields):
    body = {
        "items": [{"client_id": "a", "name": "a", "price": 20000}],
        "rule": {"minimum_amount": 30000, "unit": "PCT", "value": 10, **fields},
    }
    assert client.post(P + "/discount-previews", headers=guest_headers, json=body).status_code == 422


def test_achievement_migration_preserves_reached_goal_after_reversal(migration_connection):  # noqa: F811
    db = migration_connection
    for filename in (
        "0001_initial_schema.py",
        "0002_design_flow.py",
        "0003_purchase_questions.py",
        "0004_pre_purchase_questions.py",
        "0005_handoff_local_flow.py",
    ):
        migration(filename).upgrade()
    db.execute(text("INSERT INTO guests VALUES ('g', :hash, '2026-10-01T00:00:00Z')"), {"hash": "d" * 64})
    db.execute(
        text("""INSERT INTO goals (id, guest_id, name, target_amount, active, created_at)
        VALUES ('goal', 'g', 'goal', 10000, true, '2026-10-01T00:00:00Z')""")
    )
    db.execute(
        text("""INSERT INTO items (id, guest_id, name, price, category, purchase_reason, purchase_reasons, source, created_at)
        VALUES ('i', 'g', 'coat', 15000, 'CLOTHING', 'NECESSITY', '["NECESSITY"]', 'MANUAL', '2026-10-01T00:00:00Z')""")
    )
    db.execute(
        text("""INSERT INTO saving_events (id, guest_id, goal_id, item_id, kind, amount, occurred_on, created_at)
        VALUES ('c', 'g', 'goal', 'i', 'CREDIT', 15000, '2026-10-02', '2026-10-02T00:00:00Z'),
               ('r', 'g', 'goal', 'i', 'REVERSAL', -15000, '2026-10-03', '2026-10-03T00:00:00Z')""")
    )
    migration("0006_achieved_goal_shelf.py").upgrade()
    row = db.execute(text("SELECT achieved_on, shelf_snapshot, purchased_on FROM goals")).mappings().one()
    assert str(row["achieved_on"]) == "2026-10-02" and row["purchased_on"] is None
    assert row["shelf_snapshot"]["saved_amount"] == 15000 and row["shelf_snapshot"]["resisted_count"] == 1
