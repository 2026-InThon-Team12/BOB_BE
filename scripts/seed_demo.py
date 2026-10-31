"""Prepare independent demo history without changing the application's clock."""

import argparse
import json
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import hash_token
from app.core.clock import kst_today
from app.core.config import get_settings
from app.db import create_session_factory
from app.models import (
    Budget,
    DailyAnswer,
    Expense,
    Goal,
    Guest,
    Impulse,
    Item,
    Purchase,
    PurchaseReview,
    SavingEvent,
    Simulation,
    SimulationExtension,
)
from app.services.ai import default_question
from app.services.simulations import duration_for_price
from app.services.finance import month_days, wallet_summary


def seed_demo(session: Session, today: date, guest_id: str | None = None) -> dict:
    access_token = None
    if guest_id:
        guest = session.get(Guest, guest_id)
        if guest is None:
            raise ValueError("Guest does not exist")
    else:
        access_token = secrets.token_urlsafe(32)
        guest = Guest(token_hash=hash_token(access_token))
        session.add(guest)
        session.flush()
    ids = {}

    def item_with_history(name, price, start, answers, category="HOBBY", reasons=None):
        reasons = reasons or ["LONG_WANTED"]
        item = Item(
            guest_id=guest.id,
            name=name,
            price=price,
            category=category,
            purchase_reason=reasons[0],
            purchase_reasons=reasons,
            source="MANUAL",
            created_at=datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        )
        session.add(item)
        session.flush()
        simulation = Simulation(
            guest_id=guest.id,
            item_id=item.id,
            price_snapshot=price,
            duration_days=duration_for_price(price),
            base_duration_days=duration_for_price(price),
            start_date=start,
            end_date=start + timedelta(days=duration_for_price(price)),
            measurement_question=default_question(item.category, name),
            supplemental_question=default_question(item.category, name),
        )
        session.add(simulation)
        session.flush()
        for offset, answer in enumerate(answers):
            session.add(
                DailyAnswer(
                    simulation_id=simulation.id,
                    answer_date=start + timedelta(days=offset),
                    usage_answer=answer,
                    supplemental_answer="UNSURE",
                )
            )
        return item, simulation

    active, _ = item_with_history(
        "[데모] 코트",
        189000,
        today - timedelta(days=8),
        ["YES", "NO"] * 4,
        "CLOTHING",
        ["ADVERTISEMENT", "DISCOUNT_LIMITED"],
    )
    earbuds, _ = item_with_history(
        "[데모] 무선 이어폰", 239000, today - timedelta(days=2), ["YES", "NO"], "DIGITAL"
    )
    lamp, _ = item_with_history("[데모] 무드등", 29000, today - timedelta(days=1), ["NO"], "FURNITURE")
    completed, _ = item_with_history(
        "[데모] 한정판 스니커즈",
        159000,
        today - timedelta(days=14),
        ["YES"] * 3 + ["NO"] * 9 + ["UNSURE"] * 2,
        "CLOTHING",
        ["DISCOUNT_LIMITED", "SOCIAL"],
    )
    # A 7-day item extended by another 7 days produces the design's 14-day prediction.
    purchased, keyboard_simulation = item_with_history(
        "[데모] 무선 키보드",
        89000,
        today - timedelta(days=28),
        ["YES"] * 11 + ["NO"] * 3,
        "DIGITAL",
        ["NECESSITY"],
    )
    session.add(
        SimulationExtension(
            simulation_id=keyboard_simulation.id,
            idempotency_key="demo-extension",
            start_date=today - timedelta(days=21),
            end_date=today - timedelta(days=14),
        )
    )
    keyboard_simulation.duration_days = 14
    keyboard_simulation.end_date = today - timedelta(days=14)
    purchased.decision = "BUY"
    purchased.decided_on = today - timedelta(days=14)
    purchase = Purchase(
        guest_id=guest.id,
        item_id=purchased.id,
        purchased_on=today - timedelta(days=14),
        actual_price=89000,
        review_due_on=today,
        review_start=today - timedelta(days=14),
        review_end=today - timedelta(days=1),
    )
    session.add(purchase)
    session.flush()
    session.add(
        Expense(
            guest_id=guest.id,
            source="PURCHASE",
            purchase_id=purchase.id,
            name=purchased.name,
            amount=purchase.actual_price,
            spent_on=purchase.purchased_on,
        )
    )
    # Historical, already reviewed clothing gives a real calibration sample: 11 / 2.5 = 4.4x.
    previous, _ = item_with_history(
        "[데모] 이전 재킷",
        159000,
        today - timedelta(days=56),
        ["YES"] * 11 + ["NO"] * 3,
        "CLOTHING",
        ["ADVERTISEMENT"],
    )
    previous.decision, previous.decided_on = "BUY", today - timedelta(days=42)
    prior_purchase = Purchase(
        guest_id=guest.id,
        item_id=previous.id,
        purchased_on=previous.decided_on,
        actual_price=159000,
        review_due_on=today - timedelta(days=28),
        review_start=previous.decided_on,
        review_end=today - timedelta(days=29),
    )
    session.add(prior_purchase)
    session.flush()
    session.add(PurchaseReview(purchase_id=prior_purchase.id, frequency="SOMETIMES", submitted_on=today))
    session.add(
        Expense(
            guest_id=guest.id,
            source="PURCHASE",
            purchase_id=prior_purchase.id,
            name=previous.name,
            amount=prior_purchase.actual_price,
            spent_on=prior_purchase.purchased_on,
        )
    )
    month = today.strftime("%Y-%m")
    if session.scalar(select(Budget.id).where(Budget.guest_id == guest.id, Budget.month == month)) is None:
        session.add(
            Budget(
                guest_id=guest.id,
                month=month,
                income=1_000_000,
                food=250_000,
                living=420_000 - 5800 * month_days(month),
                transport=50_000,
                savings=5800 * month_days(month),
            )
        )
    goal = session.scalar(select(Goal).where(Goal.guest_id == guest.id, Goal.active.is_(True)))
    if goal is None:
        goal = Goal(
            guest_id=guest.id,
            name="[데모] 미쿠 피규어",
            target_amount=240000,
            funding={"startsOn": today.isoformat(), "openingBalance": 0, "baselineSpent": 168000, "reservedThisMonth": 0},
            created_at=datetime.combine(today - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc),
        )
        session.add(goal)
        session.flush()
    for index, (name, amount) in enumerate(
        [("치킨", 23000)] * 3 + [("떡볶이 배달", 18000)] * 4 + [("인형뽑기", 10000)]
    ):
        occurred = today - timedelta(days=7 - index)
        impulse = Impulse(
            guest_id=guest.id,
            goal_id=goal.id,
            name="[데모] " + name,
            amount=amount,
            decision="RESISTED",
            idempotency_key="demo-" + secrets.token_hex(16),
            request_hash="0" * 64,
            budget_month=occurred.strftime("%Y-%m"),
            available_amount=None,
            delay_days=None,
            comparison_reason="BUDGET_REQUIRED",
        )
        session.add(impulse)
        session.flush()
        session.add(
            SavingEvent(
                guest_id=guest.id,
                goal_id=goal.id,
                impulse_id=impulse.id,
                kind="CREDIT",
                amount=amount,
                occurred_on=occurred,
            )
        )
    for name, amount in (("인형뽑기", 8000), ("편의점 간식", 4500)):
        session.add(
            Expense(guest_id=guest.id, source="MANUAL", name="[데모] " + name, amount=amount, spent_on=today)
        )
    session.flush()
    remaining_demo_spend = 168000 - wallet_summary(session, guest.id, month)["spent_amount"]
    if remaining_demo_spend > 0:
        session.add(
            Expense(
                guest_id=guest.id,
                source="MANUAL",
                name="[데모] 이번 달 기타 지출",
                amount=remaining_demo_spend,
                spent_on=today,
            )
        )
    session.commit()
    ids.update(
        active_item_id=active.id,
        active_item_ids=[active.id, earbuds.id, lamp.id],
        completed_item_id=completed.id,
        purchase_id=purchase.id,
        goal_id=goal.id,
    )
    return {"guest_id": guest.id, "access_token": access_token, "demo_date": today.isoformat(), **ids}


def main() -> int:
    parser = argparse.ArgumentParser(description="Add sample data; existing data is preserved.")
    parser.add_argument("--guest-id", help="Use an existing app guest; does not print or replace its token.")
    args = parser.parse_args()
    settings = get_settings()
    if settings.environment not in {"development", "demo", "test"}:
        parser.error("Demo seed is disabled outside development/demo/test environments")
    factory = create_session_factory(settings.database_url)
    with factory() as session:
        try:
            result = seed_demo(session, kst_today(), args.guest_id)
        except ValueError as error:
            parser.error(str(error))
    # An intentionally issued demo guest token, not a provider key or server credential.
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
