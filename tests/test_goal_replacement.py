from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text

from app.models import Expense, Goal, GoalReplacement, GoalSavingTransfer, SavingEvent
from app.services.finance import saved_amount
from tests.test_finance import _budget, _decide, _goal, _impulse
from tests.test_migration_upgrade import migration, migration_connection  # noqa: F401

P = "/api/v1"


def replace(client, headers, previous, *, target=200000, carry=True, key="replace-goal-001"):
    return client.post(
        P + "/goals/current/replacement",
        headers={**headers, "Idempotency-Key": key},
        json={
            "current_goal_id": previous["id"],
            "name": "새 목표",
            "target_amount": target,
            "source_url": "https://shop.example.com/new",
            "carry_savings": carry,
        },
    )


@pytest.mark.parametrize(
    "target,carry,moved,retained",
    [
        (200000, True, 151000, 0),
        (100000, True, 100000, 51000),
        (200000, False, 0, 151000),
    ],
)
def test_replacement_preserves_money_without_new_savings_or_expenses(
    client,
    guest_headers,
    session,
    today,
    target,
    carry,
    moved,
    retained,
):
    _budget(client, guest_headers, savings=310000)
    previous = _goal(client, guest_headers, target=240000)
    impulse = _impulse(client, guest_headers, amount=151000)
    _decide(client, guest_headers, impulse, "RESISTED")
    response = replace(client, guest_headers, previous, target=target, carry=carry)
    assert response.status_code == 201, response.text
    result = response.json()
    goal = result["goal"]
    assert (result["carried_amount"], result["retained_amount"]) == (moved, retained)
    assert goal["id"] != previous["id"] and goal["saved_amount"] == moved
    assert goal["remaining_amount"] == target - moved and goal["source_url"].endswith("/new")
    assert goal["remaining_days"] == (target - moved + 9999) // 10000
    assert goal["resisted_count"] == (1 if moved else 0)
    old = session.get(Goal, previous["id"])
    assert not old.active and old.achieved_on is None and old.name == previous["name"]
    assert saved_amount(session, old.id, old.guest_id) == retained
    assert session.scalar(select(func.sum(SavingEvent.amount))) == 151000
    assert session.scalar(select(func.count(SavingEvent.id))) == 1
    assert session.scalar(select(func.count(Expense.id))) == 0
    assert (
        client.get(P + "/reports", headers=guest_headers, params={"month": "2026-10"}).json()["saved_amount"]
        == 151000
    )
    shelf = client.get(P + "/goals/shelf", headers=guest_headers).json()
    assert shelf["count"] == (1 if moved == target else 0)
    if moved == target:
        assert shelf["items"][0]["id"] == goal["id"]
        assert goal["achieved_on"] == today.value.isoformat()
    assert replace(client, guest_headers, previous, target=target, carry=carry).json() == result
    assert session.scalar(select(func.count(GoalReplacement.id))) == 1
    assert replace(client, guest_headers, previous, target=target + 1, carry=carry).status_code == 409


def test_cancellation_follows_partial_credit_transfers_and_keeps_achievement(client, guest_headers, session):
    previous = _goal(client, guest_headers, target=240000)
    first = _impulse(client, guest_headers, amount=60000)
    second = _impulse(client, guest_headers, amount=91000, key="second-credit-001")
    _decide(client, guest_headers, first, "RESISTED")
    _decide(client, guest_headers, second, "RESISTED")
    result = replace(client, guest_headers, previous, target=100000).json()
    assert result["retained_amount"] == 51000
    assert session.scalar(select(func.count(GoalSavingTransfer.id))) == 2
    _decide(client, guest_headers, second, "CANCELLED")
    current = client.get(P + "/goals/current", headers=guest_headers).json()
    assert current["saved_amount"] == 60000 and current["resisted_count"] == 1
    assert current["achieved"] and not current["currently_funded"]
    old = session.get(Goal, previous["id"])
    assert saved_amount(session, old.id, old.guest_id) == 0
    assert client.get(P + "/goals/shelf", headers=guest_headers).json()["items"][0]["saved_amount"] == 100000
    _decide(client, guest_headers, first, "CANCELLED")
    assert client.get(P + "/goals/current", headers=guest_headers).json()["saved_amount"] == 0
    assert session.scalar(select(func.sum(SavingEvent.amount))) == 0


def test_repeated_replacements_do_not_duplicate_credit_and_reverse_whole_chain(
    client, guest_headers, session
):
    first = _goal(client, guest_headers, target=240000)
    impulse = _impulse(client, guest_headers, amount=30000)
    _decide(client, guest_headers, impulse, "RESISTED")
    second = replace(client, guest_headers, first).json()["goal"]
    third = replace(client, guest_headers, second, target=100000, key="replace-goal-002").json()["goal"]
    assert third["saved_amount"] == 30000 and third["resisted_count"] == 1
    assert replace(client, guest_headers, first, key="stale-request-001").status_code == 409
    _decide(client, guest_headers, impulse, "CANCELLED")
    for goal in session.scalars(select(Goal)):
        assert saved_amount(session, goal.id, goal.guest_id) == 0


def test_replacement_ownership_and_required_idempotency(client, guest_headers, session):
    goal = _goal(client, guest_headers)
    other = client.post(P + "/guest-sessions").json()
    h = {"Authorization": "Bearer " + other["access_token"]}
    assert replace(client, h, goal).status_code == 409
    assert (
        client.post(
            P + "/goals/current/replacement",
            headers=guest_headers,
            json={"current_goal_id": goal["id"], "name": "새 목표", "target_amount": 10000},
        ).status_code
        == 422
    )
    assert session.scalar(select(func.count(Goal.id))) == 1
    impulse = _impulse(client, guest_headers, amount=100000)
    _decide(client, guest_headers, impulse, "RESISTED")
    assert replace(client, guest_headers, goal).json()["error"]["code"] == "GOAL_ALREADY_ACHIEVED"


def test_concurrent_replacement_is_exactly_once(client, guest_headers, session):
    if session.bind.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    previous = _goal(client, guest_headers)
    impulse = _impulse(client, guest_headers, amount=30000)
    _decide(client, guest_headers, impulse, "RESISTED")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: replace(client, guest_headers, previous), range(4)))
    assert all(r.status_code == 201 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)
    assert session.scalar(select(func.count(GoalReplacement.id))) == 1
    assert session.scalar(select(func.count(GoalSavingTransfer.id))) == 1
    assert session.scalar(select(func.count(Goal.id)).where(Goal.active.is_(True))) == 1


def test_replacement_migration_keeps_existing_ledger_and_protects_history(migration_connection):  # noqa: F811
    db = migration_connection
    for filename in (
        "0001_initial_schema.py",
        "0002_design_flow.py",
        "0003_purchase_questions.py",
        "0004_pre_purchase_questions.py",
        "0005_handoff_local_flow.py",
        "0006_achieved_goal_shelf.py",
    ):
        migration(filename).upgrade()
    db.execute(text("INSERT INTO guests VALUES ('g', :hash, now())"), {"hash": "f" * 64})
    db.execute(
        text("""INSERT INTO goals (id, guest_id, name, target_amount, active, created_at)
        VALUES ('old', 'g', 'old', 240000, false, now()), ('new', 'g', 'new', 200000, true, now())""")
    )
    db.execute(
        text("""INSERT INTO items (id, guest_id, name, price, category, purchase_reason, purchase_reasons, source, created_at)
        VALUES ('i', 'g', 'coat', 30000, 'CLOTHING', 'NECESSITY', '["NECESSITY"]', 'MANUAL', now())""")
    )
    db.execute(
        text("""INSERT INTO saving_events (id, guest_id, goal_id, item_id, kind, amount, occurred_on, created_at)
        VALUES ('c', 'g', 'old', 'i', 'CREDIT', 30000, '2026-10-31', now())""")
    )
    change = migration("0007_goal_replacements.py")
    change.upgrade()
    assert db.execute(text("SELECT goal_id, amount FROM saving_events")).one() == ("old", 30000)
    db.execute(
        text("""INSERT INTO goal_replacements (id, guest_id, from_goal_id, to_goal_id,
        idempotency_key, request_hash, carry_savings, response, created_at)
        VALUES ('r', 'g', 'old', 'new', 'request-001', :hash, true, '{}', now())"""),
        {"hash": "a" * 64},
    )
    db.execute(
        text("""INSERT INTO goal_saving_transfers (id, guest_id, replacement_id, credit_id,
        from_goal_id, to_goal_id, amount, created_at) VALUES ('t', 'g', 'r', 'c', 'old', 'new', 30000, now())""")
    )
    with pytest.raises(RuntimeError, match="goal replacement history"):
        change.downgrade()
    assert db.scalar(text("SELECT amount FROM goal_saving_transfers")) == 30000
