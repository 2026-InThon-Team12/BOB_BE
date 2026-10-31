"""The four app tabs: home, goals, reports and wallet."""

from datetime import date, timedelta
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.errors import AppError
from app.db import get_session
from app.models import Budget, Expense, Guest, Item
from app.schemas import RequestModel, Money, Name, TodayOutput, ItemOutput
from app.api.finance import GoalOutput, BudgetInput
from app.services.finance import (
    active_goal,
    record_goal_achievement,
    goal_summary,
    lock_guest,
    month_days,
    request_hash,
    wallet_summary,
    compare_wallet,
)
from app.services.reports import monthly_report
from app.services.simulations import get_today_cards, item_view

router = APIRouter(tags=["app screens"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


class ExpenseInput(RequestModel):
    name: Name
    amount: Money
    spent_on: date
    memo: str | None = Field(default=None, max_length=500)


class ExpenseOutput(ExpenseInput):
    id: str
    source: str
    voided: bool


class WalletSummary(BaseModel):
    month: str
    budget_required: bool
    needs_monthly_confirmation: bool
    free_spending_amount: int | None
    spent_amount: int
    remaining_amount: int | None
    planned_savings: int | None
    daily_savings: float | None
    is_account_balance: bool


class WalletOutput(WalletSummary):
    plan: BudgetInput | None


class WalletComparison(BaseModel):
    remaining_amount: int | None
    remaining_after_purchase: int | None
    over_budget_amount: int | None
    budget_required: bool


class HomeOutput(BaseModel):
    today: TodayOutput
    goal: GoalOutput | None
    wallet: WalletSummary
    simulations: list[ItemOutput]


class ExpenseList(BaseModel):
    items: list[ExpenseOutput]
    month: str
    limit: int
    offset: int


class CategoryStatistic(BaseModel):
    category: str
    sample_count: int
    predicted_days: float | None
    actual_days_estimate: float
    actual_days_min: float
    actual_days_max: float
    is_estimate: bool
    overprediction_ratio_estimate: float | None
    status: str
    metric_unit: str
    measurement_count: int
    observed_days: int


class Discovery(CategoryStatistic):
    message: str


class ReasonStatistic(BaseModel):
    reason: str
    registered_count: int
    decided_count: int
    abandoned_count: int
    abandonment_rate: float | None


class ReportOutput(BaseModel):
    month: str
    registered_count: int
    bye_count: int
    saved_amount: int
    categories: list[CategoryStatistic]
    purchase_reasons: list[ReasonStatistic]
    discovery: Discovery | None
    metric_unit: str
    estimate_method: str
    empty: bool


def expense_output(expense: Expense) -> dict:
    return {
        key: getattr(expense, key) for key in ("id", "source", "name", "amount", "spent_on", "memo", "voided")
    }


def checked_month(month: str | None, today: date) -> str:
    month = month or today.strftime("%Y-%m")
    try:
        month_days(month)
        if month == "9999-12":
            raise ValueError()
    except ValueError:
        raise AppError(422, "INVALID_MONTH", "유효한 YYYY-MM 월을 입력해 주세요.") from None
    return month


@router.get("/home", response_model=HomeOutput)
def home(session: DB, guest: Owner, today: Today) -> dict:
    lock_guest(session, guest.id)
    goal = active_goal(session, guest.id)
    record_goal_achievement(session, goal, today)
    session.commit()
    items = list(
        session.scalars(
            select(Item)
            .where(Item.guest_id == guest.id, Item.decision.is_(None) | (Item.decision == "HOLD"))
            .order_by(Item.created_at.desc())
            .limit(100)
        )
    )
    return {
        "today": get_today_cards(session, guest.id, today),
        "goal": goal_summary(session, goal, today) if goal else None,
        "wallet": wallet_summary(session, guest.id, today.strftime("%Y-%m"), today),
        "simulations": [item_view(session, item, today) for item in items],
    }


@router.get("/wallet", response_model=WalletOutput)
def wallet(session: DB, guest: Owner, today: Today, month: str | None = None) -> dict:
    month = checked_month(month, today)
    budget = session.scalar(select(Budget).where(Budget.guest_id == guest.id, Budget.month == month))
    return {
        **wallet_summary(session, guest.id, month, today),
        "plan": {
            key: getattr(budget, key)
            for key in ("income", "food", "living", "transport", "savings", "hourly_wage")
        }
        if budget
        else None,
    }


@router.get("/wallet/compare", response_model=WalletComparison)
def wallet_compare(
    session: DB, guest: Owner, today: Today, price: int = Query(gt=0, le=1_000_000_000_000)
) -> dict:
    return compare_wallet(session, guest.id, today, price)


@router.get("/expenses", response_model=ExpenseList)
def expenses(
    session: DB,
    guest: Owner,
    today: Today,
    month: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    month = checked_month(month, today)
    start = date.fromisoformat(month + "-01")
    rows = session.scalars(
        select(Expense)
        .where(
            Expense.guest_id == guest.id,
            Expense.voided.is_(False),
            Expense.spent_on >= start,
            Expense.spent_on < start + timedelta(days=month_days(month)),
        )
        .order_by(Expense.spent_on.desc(), Expense.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"items": [expense_output(row) for row in rows], "month": month, "limit": limit, "offset": offset}


@router.post("/expenses", response_model=ExpenseOutput, status_code=201)
def add_expense(body: ExpenseInput, idempotency_key: Key, session: DB, guest: Owner, today: Today):
    lock_guest(session, guest.id)
    fingerprint = request_hash(body.model_dump(mode="json"))
    existing = session.scalar(
        select(Expense).where(Expense.guest_id == guest.id, Expense.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 키로 다른 지출을 등록할 수 없습니다.")
        return expense_output(existing)
    if body.spent_on > today:
        raise AppError(422, "FUTURE_EXPENSE", "아직 쓰지 않은 지출은 기록할 수 없습니다.")
    expense = Expense(
        guest_id=guest.id,
        source="MANUAL",
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        **body.model_dump(),
    )
    session.add(expense)
    session.commit()
    return expense_output(expense)


def owned_manual(session: Session, guest_id: str, expense_id: str) -> Expense:
    expense = session.scalar(select(Expense).where(Expense.id == expense_id, Expense.guest_id == guest_id))
    if expense is None:
        raise AppError(404, "EXPENSE_NOT_FOUND", "지출 기록을 찾을 수 없습니다.")
    if expense.source != "MANUAL":
        raise AppError(409, "LINKED_EXPENSE", "구매로 생성된 지출은 수동 지출 화면에서 변경할 수 없습니다.")
    return expense


@router.put("/expenses/{expense_id}", response_model=ExpenseOutput)
def edit_expense(expense_id: str, body: ExpenseInput, session: DB, guest: Owner, today: Today):
    lock_guest(session, guest.id)
    expense = owned_manual(session, guest.id, expense_id)
    if expense.voided or body.spent_on > today:
        raise AppError(409, "EXPENSE_NOT_EDITABLE", "삭제한 기록이나 미래 날짜는 수정할 수 없습니다.")
    for key, value in body.model_dump().items():
        setattr(expense, key, value)
    session.commit()
    return expense_output(expense)


@router.delete("/expenses/{expense_id}", status_code=204)
def remove_expense(expense_id: str, session: DB, guest: Owner):
    lock_guest(session, guest.id)
    expense = owned_manual(session, guest.id, expense_id)
    expense.voided = True
    session.commit()
    return Response(status_code=204)


@router.get("/reports", response_model=ReportOutput)
def reports(session: DB, guest: Owner, today: Today, month: str | None = None) -> dict:
    return monthly_report(session, guest.id, checked_month(month, today), today)


@router.get("/impulses/presets")
def impulse_presets() -> dict:
    return {
        "items": [
            {"name": name, "amount": amount}
            for name, amount in (("커피", 4800), ("치킨", 23000), ("떡볶이 배달", 18000), ("인형뽑기", 10000))
        ]
    }
