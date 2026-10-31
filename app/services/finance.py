"""Deterministic budget calculations and virtual savings helpers.

Money remains integer KRW. In particular, delay estimates never use the rounded
daily amount shown to the client.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services import goal_funding
from app.models import Budget, Expense, Goal, GoalSavingTransfer, Guest, Impulse, Item, SavingEvent


@dataclass(frozen=True)
class BudgetComparison:
    budget_month: str
    available_amount: int | None
    delay_days: int | None
    comparison_reason: str | None


def month_days(month: str) -> int:
    """Return the actual calendar length of a strict YYYY-MM value."""
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must use YYYY-MM")
    year, number = (int(part) for part in month.split("-"))
    if year < 1:
        raise ValueError("year must be between 0001 and 9999")
    return calendar.monthrange(year, number)[1]


def compare_spending(
    amount: int,
    month: str,
    *,
    income: int | None = None,
    food: int = 0,
    living: int = 0,
    transport: int = 0,
    savings: int = 0,
    spent: int = 0,
    today: date | None = None,
) -> BudgetComparison:
    """Compare spending with potential saving capacity, not account balance."""
    days = month_days(month)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("amount must be a positive integer")
    if income is None:
        return BudgetComparison(month, None, None, "BUDGET_REQUIRED")
    components = (income, food, living, transport, savings)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in components):
        raise ValueError("budget components must be nonnegative integers")
    available = income - food - living - transport - savings - spent
    if available <= 0:
        return BudgetComparison(month, available, None, "NO_AVAILABLE_BUDGET")
    remaining_days = days - today.day + 1 if today and today.strftime("%Y-%m") == month else days
    delay = max(1, (amount * remaining_days + available - 1) // available)
    return BudgetComparison(month, available, delay, None)


def request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def lock_guest(session: Session, guest_id: str) -> None:
    """Serialize finance mutations for an owner, including first-row creation."""
    session.execute(select(Guest.id).where(Guest.id == guest_id).with_for_update()).scalar_one()


def active_goal(session: Session, guest_id: str) -> Goal | None:
    return session.scalar(select(Goal).where(Goal.guest_id == guest_id, Goal.active.is_(True)))


def goal_credit_balances(session: Session, goal_id: str, guest_id: str) -> list[tuple[SavingEvent, int]]:
    """Remaining allocation by original credit, oldest first.

    A reversal invalidates every allocation of that credit, including transfers
    through several goals. The signed savings ledger remains unchanged.
    """
    events = list(
        session.scalars(
            select(SavingEvent)
            .where(SavingEvent.guest_id == guest_id)
            .order_by(SavingEvent.created_at, SavingEvent.id)
        )
    )
    cancelled = {(e.impulse_id, e.item_id) for e in events if e.kind == "REVERSAL"}
    credits = {e.id: e for e in events if e.kind == "CREDIT" and (e.impulse_id, e.item_id) not in cancelled}
    balances = {key: e.amount if e.goal_id == goal_id else 0 for key, e in credits.items()}
    transfers = session.scalars(
        select(GoalSavingTransfer).where(
            GoalSavingTransfer.guest_id == guest_id,
            (GoalSavingTransfer.from_goal_id == goal_id) | (GoalSavingTransfer.to_goal_id == goal_id),
        )
    )
    for transfer in transfers:
        if transfer.credit_id in balances:
            balances[transfer.credit_id] += (
                transfer.amount if transfer.to_goal_id == goal_id else -transfer.amount
            )
    return [(credits[key], amount) for key, amount in balances.items() if amount > 0]


def saved_amount(session: Session, goal_id: str, guest_id: str) -> int:
    goal = session.get(Goal, goal_id)
    if goal and goal.guest_id == guest_id and goal.funding:
        from app.core.clock import kst_today

        return goal_funding.plan(session, goal, kst_today())["saved"]
    return sum(amount for _, amount in goal_credit_balances(session, goal_id, guest_id))


def safe_eta(today: date, days: int | None) -> date | None:
    if days is None or days > (date.max - today).days:
        return None
    return today + timedelta(days=days)


def goal_summary(session: Session, goal: Goal, today: date | None = None) -> dict[str, Any]:
    from app.core.clock import SEOUL, kst_today

    today = today or kst_today()
    balances = goal_credit_balances(session, goal.id, goal.guest_id)
    saved = sum(amount for _, amount in balances)
    result = goal_funding.plan(session, goal, today) if goal.funding else None
    if result:
        saved = result["saved"]
    daily = result["daily"] if result else None
    left = max(0, goal.target_amount - saved)
    eta = result["eta"] if result else goal.achieved_on
    eta_days = max(0, (eta - today).days) if eta else None
    credits = [event for event, _ in balances]
    labels = {}
    for event in credits:
        source = (
            session.get(Impulse, event.impulse_id) if event.impulse_id else session.get(Item, event.item_id)
        )
        if source:
            labels[source.name] = labels.get(source.name, 0) + 1
    created = goal.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "id": goal.id,
        "name": goal.name,
        "target_amount": goal.target_amount,
        "active": goal.active,
        "saved_amount": saved,
        "remaining_amount": left,
        "progress_percent": min(100.0, max(0.0, round(saved * 100 / goal.target_amount, 2))),
        "achieved": goal.achieved_on is not None or saved >= goal.target_amount,
        "achieved_on": goal.achieved_on,
        "currently_funded": saved >= goal.target_amount,
        "is_virtual": True,
        "daily_savings": daily,
        "estimated_completion_on": eta,
        "funding_method": "CALENDAR_BUDGET" if goal.funding else "LEGACY_VIRTUAL",
        "uses_previous_budget": result["uses_previous_budget"] if result else False,
        "remaining_days": eta_days,
        "resisted_count": len(credits),
        "elapsed_days": max(
            1, ((goal.achieved_on or goal.completed_on or today) - created.astimezone(SEOUL).date()).days + 1
        ),
        "most_resisted_item": max(labels, key=labels.get) if labels else None,
        "image_url": goal.image_url,
        "source_url": goal.source_url,
        "can_shop": goal.source_url is not None,
        "shelf_count": session.scalar(
            select(func.count(Goal.id)).where(Goal.guest_id == goal.guest_id, Goal.achieved_on.is_not(None))
        ),
        "shelf_url": "/api/v1/goals/shelf",
    }


def record_goal_achievement(session: Session, goal: Goal | None, today: date) -> None:
    """Settle completed calendar days inside the owner's transaction exactly once."""
    if goal is None or goal.achieved_on is not None:
        return
    session.flush()
    result = goal_funding.plan(session, goal, today) if goal.funding else None
    saved = result["saved"] if result else saved_amount(session, goal.id, goal.guest_id)
    if saved < goal.target_amount:
        return
    if result:
        goal_funding.freeze(goal, result, today)
    goal.achieved_on = (result["achieved_on"] if result else None) or today
    goal.completed_on = goal.completed_on or today
    session.flush()
    summary = goal_summary(session, goal, today)
    goal.shelf_snapshot = {
        key: summary[key] for key in ("saved_amount", "resisted_count", "elapsed_days", "most_resisted_item")
    }


def wallet_summary(session: Session, guest_id: str, month: str, today: date | None = None) -> dict:
    from app.core.clock import kst_today

    today = today or kst_today()
    days = month_days(month)
    start = date.fromisoformat(month + "-01")
    end = start + timedelta(days=days)
    budget = session.scalar(select(Budget).where(Budget.guest_id == guest_id, Budget.month == month))
    spent = int(
        session.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.guest_id == guest_id,
                Expense.spent_on >= start,
                Expense.spent_on < end,
                Expense.voided.is_(False),
            )
        )
        or 0
    )
    available = (
        budget.income - budget.food - budget.living - budget.transport - budget.savings if budget else None
    )
    return {
        "month": month,
        "budget_required": budget is None,
        "needs_monthly_confirmation": budget is None,
        "free_spending_amount": available,
        "spent_amount": spent,
        "remaining_amount": available - spent if available is not None else None,
        "planned_savings": budget.savings if budget else None,
        "daily_savings": max(0, available - spent)
        / (days - today.day + 1 if today.strftime("%Y-%m") == month else days)
        if available is not None and available - spent > 0
        else None,
        "is_account_balance": False,
    }


def compare_wallet(session: Session, guest_id: str, today: date, price: int) -> dict:
    wallet = wallet_summary(session, guest_id, today.strftime("%Y-%m"), today)
    remaining = wallet["remaining_amount"]
    return {
        "remaining_amount": remaining,
        "remaining_after_purchase": remaining - price if remaining is not None else None,
        "over_budget_amount": max(0, price - remaining) if remaining is not None else None,
        "budget_required": remaining is None,
    }


def attach_unallocated_savings(session: Session, goal: Goal) -> None:
    for entry in session.scalars(
        select(SavingEvent).where(SavingEvent.guest_id == goal.guest_id, SavingEvent.goal_id.is_(None))
    ):
        entry.goal_id = goal.id
