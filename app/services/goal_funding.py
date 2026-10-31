"""Calendar saving estimates; resisting a purchase never creates money.

The opening snapshot excludes historical virtual credits. Each completed day
allocates only its share of the remaining monthly discretionary budget. Future
months repeat the latest entered budget and are explicitly estimates.
"""

from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import select
from app.models import Budget, Expense

DAY = timedelta(days=1)
HOURLY_RATE = 10_700


def free_budget(budget):
    return budget.income - budget.food - budget.living - budget.transport - budget.savings


def inputs(session, guest_id):
    budgets = {
        b.month: free_budget(b) for b in session.scalars(select(Budget).where(Budget.guest_id == guest_id))
    }
    expenses = [
        (e.spent_on, e.amount)
        for e in session.scalars(
            select(Expense).where(Expense.guest_id == guest_id, Expense.voided.is_(False))
        )
    ]
    return budgets, expenses


def opening(session, guest_id, today, balance=0, reserved=0):
    _, expenses = inputs(session, guest_id)
    spent = sum(
        amount for on, amount in expenses if on.strftime("%Y-%m") == today.strftime("%Y-%m") and on <= today
    )
    return dict(
        startsOn=today.isoformat(), openingBalance=balance, baselineSpent=spent, reservedThisMonth=reserved
    )


def calculate(target, funding, budgets, expenses, today, extra_spending=0):
    start = date.fromisoformat(funding["startsOn"])
    frozen = date.fromisoformat(funding["frozenOn"]) if funding.get("frozenOn") else None
    end = min(today, frozen) if frozen else today
    saved, pool, allocated = funding["openingBalance"], 0, funding["reservedThisMonth"]
    month, achieved_on, estimated = "", None, False
    amounts = {}
    for on, amount in expenses:
        amounts[on] = amounts.get(on, 0) + amount
    first_month = start.strftime("%Y-%m")
    before = sum(amount for on, amount in expenses if on.strftime("%Y-%m") == first_month and on < start)

    def capacity(m):
        nonlocal estimated
        available = [key for key in budgets if key <= m]
        if not available:
            return None
        key = max(available)
        estimated |= key != m
        return budgets[key]

    day = start
    while day <= end:
        current = day.strftime("%Y-%m")
        if current != month:
            month = current
            allocated = funding["reservedThisMonth"] if month == first_month else 0
            pool = max(
                0,
                (capacity(month) or 0)
                - (funding["baselineSpent"] + allocated if month == first_month else 0),
            )
        spend = (
            amounts.get(day, 0)
            + (before - funding["baselineSpent"] if day == start else 0)
            + (extra_spending if day == today else 0)
        )
        pool -= spend
        if pool < 0:
            debit = min(saved, -pool)
            saved -= debit
            allocated = max(0, allocated - debit)
            pool = 0
        if day < today and frozen is None:
            credit = pool // (monthrange(day.year, day.month)[1] - day.day + 1)
            pool -= credit
            saved += credit
            allocated += credit
            if saved >= target:
                achieved_on = day
                break
        if day == date.max:
            break
        day += DAY
    saved = funding.get("frozenBalance", saved)
    allocated = funding.get("frozenAllocated", allocated)
    remaining = max(0, target - saved)
    eta = (frozen or achieved_on or today) if remaining == 0 else None
    daily = (
        pool / (monthrange(today.year, today.month)[1] - today.day + 1)
        if capacity(today.strftime("%Y-%m")) is not None and pool > 0 and remaining > 0 and frozen is None
        else None
    )
    left, cursor, available = remaining, today, pool
    for _ in range(1200):
        if left <= 0:
            break
        days = monthrange(cursor.year, cursor.month)[1] - cursor.day + 1
        if available >= left and available > 0:
            for offset in range(days):
                credit = available // (days - offset)
                available -= credit
                left -= credit
                if left <= 0:
                    eta = cursor + timedelta(days=offset)
                    break
            break
        left -= available
        if days > (date.max - cursor).days:
            break
        cursor += timedelta(days=days)
        available = max(0, capacity(cursor.strftime("%Y-%m")) or 0)
        if available <= 0:
            break
    return dict(
        saved=saved,
        remaining=remaining,
        eta=eta,
        daily=daily,
        achieved_on=achieved_on,
        month_allocated=allocated,
        allocation_month=month,
        uses_previous_budget=estimated,
    )


def plan(session, goal, today, extra_spending=0):
    budgets, expenses = inputs(session, goal.guest_id)
    return calculate(goal.target_amount, goal.funding, budgets, expenses, today, extra_spending)


def freeze(goal, result, today, retained=None):
    goal.funding = {
        **goal.funding,
        "frozenBalance": result["saved"] if retained is None else retained,
        "frozenAllocated": result["month_allocated"],
        "frozenOn": (result["achieved_on"] or today).isoformat(),
    }
