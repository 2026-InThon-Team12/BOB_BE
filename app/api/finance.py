"""Monthly budget, one active goal, and immediate impulse intervention APIs."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.errors import AppError
from app.db import get_session
from app.models import Budget, Expense, Goal, Guest, Impulse, SavingEvent
from app.services.finance import (
    active_goal,
    compare_spending,
    goal_summary,
    lock_guest,
    month_days,
    request_hash,
    attach_unallocated_savings,
    safe_eta,
    record_goal_achievement,
)


router = APIRouter(tags=["finance"])

Money = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)]
PositiveMoney = Annotated[int, Field(strict=True, gt=0, le=1_000_000_000_000)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
Month = Annotated[str, Path(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BudgetInput(InputModel):
    income: Money
    food: Money
    living: Money
    transport: Money
    savings: Money = 0
    hourly_wage: PositiveMoney | None = None


class BudgetOutput(BudgetInput):
    month: str
    month_days: int
    monthly_available_amount: int
    daily_saving_possible_amount: float | None
    is_estimate: bool = True


def validate_product_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("Use a valid HTTPS product URL without credentials")
    _ = parsed.port
    return value


ProductUrl = Annotated[str, Field(min_length=9, max_length=2048), AfterValidator(validate_product_url)]


class GoalInput(InputModel):
    name: Name
    target_amount: PositiveMoney
    image_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    source_url: ProductUrl | None = None


class GoalPatch(InputModel):
    name: Name | None = None
    target_amount: PositiveMoney | None = None
    image_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    source_url: ProductUrl | None = None

    @model_validator(mode="after")
    def require_change(self) -> GoalPatch:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set if key != "source_url"
        ):
            raise ValueError("provide at least one non-null goal field")
        return self


class GoalOutput(BaseModel):
    id: str
    name: str
    target_amount: int
    active: bool
    saved_amount: int
    remaining_amount: int = 0
    progress_percent: float
    achieved: bool
    is_virtual: bool
    daily_savings: float | None = None
    estimated_completion_on: date | None = None
    remaining_days: int | None = None
    resisted_count: int = 0
    elapsed_days: int = 0
    most_resisted_item: str | None = None
    image_url: str | None = None
    achieved_on: date | None = None
    currently_funded: bool = False
    source_url: str | None = None
    can_shop: bool = False
    shelf_count: int = 0
    shelf_url: str = "/api/v1/goals/shelf"


class ImpulseInput(InputModel):
    name: Name
    amount: PositiveMoney


class ImpulseDecision(InputModel):
    decision: Literal["RESISTED", "PURCHASED", "CANCELLED"]


class ImpulseOutput(BaseModel):
    id: str
    name: str
    amount: int
    goal_id: str | None
    decision: str
    budget_month: str
    available_amount: int | None
    delay_days: int | None
    comparison_reason: str | None
    message: str
    if_resisted_on: date | None = None
    if_purchased_on: date | None = None
    is_estimate: bool = True


def _month_days(month: str) -> int:
    try:
        return month_days(month)
    except ValueError as exc:
        raise AppError(422, "INVALID_MONTH", "월은 유효한 YYYY-MM 형식이어야 해요.") from exc


def _budget_output(budget: Budget) -> dict:
    days = _month_days(budget.month)
    available = budget.income - budget.food - budget.living - budget.transport - budget.savings
    return {
        "month": budget.month,
        "income": budget.income,
        "food": budget.food,
        "living": budget.living,
        "transport": budget.transport,
        "savings": budget.savings,
        "hourly_wage": budget.hourly_wage,
        "month_days": days,
        "monthly_available_amount": available,
        "daily_saving_possible_amount": round(budget.savings / days, 2) if budget.savings > 0 else None,
        "is_estimate": True,
    }


def _impulse_output(impulse: Impulse) -> dict:
    if impulse.delay_days is not None:
        message = f"이번 소비로 목표가 약 {impulse.delay_days}일 늦어질 수 있어요."
        if impulse.goal_id is None:
            message = f"이번 소비는 입력한 예산 기준 약 {impulse.delay_days}일치 저축 가능액이에요."
    elif impulse.comparison_reason == "BUDGET_REQUIRED":
        message = "이번 달 예산을 입력하면 예상 지연 일수를 확인할 수 있어요."
    else:
        message = "입력한 예산에 저축 가능한 금액이 없어 지연 일수를 계산할 수 없어요."
    return {
        "id": impulse.id,
        "name": impulse.name,
        "amount": impulse.amount,
        "goal_id": impulse.goal_id,
        "decision": impulse.decision,
        "budget_month": impulse.budget_month,
        "available_amount": impulse.available_amount,
        "delay_days": impulse.delay_days,
        "comparison_reason": impulse.comparison_reason,
        "if_resisted_on": impulse.if_resisted_on,
        "if_purchased_on": impulse.if_purchased_on,
        "message": message,
        "is_estimate": True,
    }


@router.get("/budgets/{month}", response_model=BudgetOutput)
def get_budget(
    month: Month,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
) -> dict:
    _month_days(month)
    budget = session.scalar(select(Budget).where(Budget.guest_id == guest.id, Budget.month == month))
    if budget is None:
        raise AppError(404, "BUDGET_NOT_FOUND", "해당 월의 예산이 없어요.")
    return _budget_output(budget)


@router.put("/budgets/{month}", response_model=BudgetOutput)
def put_budget(
    month: Month,
    body: BudgetInput,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
) -> dict:
    _month_days(month)
    lock_guest(session, guest.id)
    budget = session.scalar(select(Budget).where(Budget.guest_id == guest.id, Budget.month == month))
    if budget is None:
        budget = Budget(guest_id=guest.id, month=month, **body.model_dump())
        session.add(budget)
    else:
        for field, value in body.model_dump().items():
            setattr(budget, field, value)
    session.commit()
    return _budget_output(budget)


@router.post("/goals", response_model=GoalOutput, status_code=201)
def create_goal(
    body: GoalInput,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
    today: date = Depends(get_today),
) -> dict:
    lock_guest(session, guest.id)
    if active_goal(session, guest.id) is not None:
        raise AppError(409, "ACTIVE_GOAL_EXISTS", "진행 중인 목표는 하나만 설정할 수 있어요.")
    goal = Goal(guest_id=guest.id, **body.model_dump(), active=True)
    session.add(goal)
    session.flush()
    attach_unallocated_savings(session, goal)
    record_goal_achievement(session, goal, today)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AppError(409, "ACTIVE_GOAL_EXISTS", "진행 중인 목표는 하나만 설정할 수 있어요.") from exc
    return goal_summary(session, goal, today)


@router.get("/goals/current", response_model=GoalOutput)
def get_current_goal(
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
    today: date = Depends(get_today),
) -> dict:
    goal = active_goal(session, guest.id)
    if goal is None:
        raise AppError(404, "GOAL_NOT_FOUND", "진행 중인 목표가 없어요.")
    return goal_summary(session, goal, today)


@router.patch("/goals/current", response_model=GoalOutput)
def patch_current_goal(
    body: GoalPatch,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
    today: date = Depends(get_today),
) -> dict:
    lock_guest(session, guest.id)
    goal = active_goal(session, guest.id)
    if goal is None:
        raise AppError(404, "GOAL_NOT_FOUND", "진행 중인 목표가 없어요.")
    if goal.achieved_on and body.model_fields_set & {"name", "target_amount"}:
        raise AppError(
            409, "GOAL_ALREADY_ACHIEVED", "달성한 목표의 이름과 금액은 유지하고 다음 목표를 정해 주세요."
        )
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(goal, field, value)
    record_goal_achievement(session, goal, today)
    session.commit()
    return goal_summary(session, goal, today)


@router.post("/impulses", response_model=ImpulseOutput, status_code=201)
def create_impulse(
    body: ImpulseInput,
    idempotency_key: IdempotencyKey,
    today: date = Depends(get_today),
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
) -> dict:
    lock_guest(session, guest.id)
    fingerprint = request_hash(body.model_dump())
    existing = session.scalar(
        select(Impulse).where(Impulse.guest_id == guest.id, Impulse.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.request_hash != fingerprint:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 중복 방지 키에 다른 요청을 사용할 수 없어요.")
        return _impulse_output(existing)
    goal = active_goal(session, guest.id)
    month = today.strftime("%Y-%m")
    budget = session.scalar(select(Budget).where(Budget.guest_id == guest.id, Budget.month == month))
    components = (
        {
            "income": budget.income,
            "food": budget.food,
            "living": budget.living,
            "transport": budget.transport,
            "savings": budget.savings,
        }
        if budget
        else {}
    )
    comparison = compare_spending(body.amount, month, **components)
    impulse = Impulse(
        guest_id=guest.id,
        goal_id=goal.id if goal else None,
        name=body.name,
        amount=body.amount,
        decision="PENDING",
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        budget_month=comparison.budget_month,
        available_amount=comparison.available_amount,
        delay_days=comparison.delay_days,
        comparison_reason=comparison.comparison_reason,
    )
    if goal:
        summary = goal_summary(session, goal, today)
        impulse.if_resisted_on = summary["estimated_completion_on"]
        impulse.if_purchased_on = (
            safe_eta(today, summary["remaining_days"] + impulse.delay_days)
            if summary["remaining_days"] is not None and impulse.delay_days is not None
            else None
        )
    session.add(impulse)
    session.commit()
    return _impulse_output(impulse)


@router.put("/impulses/{impulse_id}/decision", response_model=ImpulseOutput)
def decide_impulse(
    impulse_id: str,
    body: ImpulseDecision,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
    today: date = Depends(get_today),
) -> dict:
    lock_guest(session, guest.id)
    impulse = session.scalar(
        select(Impulse).where(Impulse.id == impulse_id, Impulse.guest_id == guest.id).with_for_update()
    )
    if impulse is None:
        raise AppError(404, "IMPULSE_NOT_FOUND", "소비 기록을 찾을 수 없어요.")
    if impulse.decision == body.decision:
        return _impulse_output(impulse)
    allowed = {
        "PENDING": {"RESISTED", "PURCHASED", "CANCELLED"},
        "RESISTED": {"CANCELLED"},
    }
    if body.decision not in allowed.get(impulse.decision, set()):
        raise AppError(409, "INVALID_DECISION_TRANSITION", "이 소비 기록은 해당 결정으로 변경할 수 없어요.")
    if body.decision == "RESISTED":
        goal = active_goal(session, guest.id)
        impulse.goal_id = goal.id if goal else None
        session.add(
            SavingEvent(
                guest_id=guest.id,
                goal_id=impulse.goal_id,
                impulse_id=impulse.id,
                kind="CREDIT",
                amount=impulse.amount,
                occurred_on=today,
            )
        )
    elif body.decision == "CANCELLED" and impulse.decision == "RESISTED":
        credit = session.scalar(
            select(SavingEvent).where(SavingEvent.impulse_id == impulse.id, SavingEvent.kind == "CREDIT")
        )
        session.add(
            SavingEvent(
                guest_id=guest.id,
                goal_id=credit.goal_id,
                impulse_id=impulse.id,
                kind="REVERSAL",
                amount=-impulse.amount,
                occurred_on=today,
            )
        )
    elif body.decision == "PURCHASED":
        session.add(
            Expense(
                guest_id=guest.id,
                source="IMPULSE",
                impulse_id=impulse.id,
                name=impulse.name,
                amount=impulse.amount,
                spent_on=today,
            )
        )
    impulse.decision = body.decision
    if body.decision == "RESISTED":
        record_goal_achievement(session, goal, today)
    session.commit()
    return _impulse_output(impulse)


@router.post("/goals/current/next", response_model=GoalOutput, status_code=201)
def next_goal(
    body: GoalInput,
    guest: Guest = Depends(get_guest),
    session: Session = Depends(get_session),
    today: date = Depends(get_today),
):
    lock_guest(session, guest.id)
    previous = active_goal(session, guest.id)
    if previous is None or not goal_summary(session, previous, today)["achieved"]:
        raise AppError(409, "GOAL_NOT_ACHIEVED", "현재 목표를 달성한 뒤 다음 목표를 정해 주세요.")
    previous.active = False
    record_goal_achievement(session, previous, today)
    previous.completed_on = previous.completed_on or today
    session.flush()
    goal = Goal(guest_id=guest.id, **body.model_dump())
    session.add(goal)
    session.flush()
    attach_unallocated_savings(session, goal)
    record_goal_achievement(session, goal, today)
    session.commit()
    return goal_summary(session, goal, today)
