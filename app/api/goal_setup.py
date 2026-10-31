"""Goal setup previews and atomic replacement with optional savings transfer."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services import goal_funding
from app.api.finance import GoalInput, GoalOutput, IdempotencyKey
from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.errors import AppError
from app.db import get_session
from app.models import Goal, GoalReplacement, Guest, Item, SavingEvent
from app.services.finance import (
    active_goal,
    goal_summary,
    lock_guest,
    record_goal_achievement,
    request_hash,
)

router = APIRouter(tags=["goal setup"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]


class GoalPreviewInput(GoalInput):
    mode: Literal["FIRST", "CHANGE", "NEXT"]
    current_goal_id: str | None = Field(default=None, min_length=1, max_length=36)
    carry_savings: bool = Field(default=True, strict=True)


class GoalReplacementInput(GoalInput):
    current_goal_id: str = Field(min_length=1, max_length=36)
    carry_savings: bool = Field(default=True, strict=True)


class GoalCandidate(BaseModel):
    item_id: str
    name: str
    target_amount: int
    source_url: str | None
    image_url: str | None


class GoalSetupOutput(BaseModel):
    mode: Literal["FIRST", "CHANGE", "NEXT"]
    current_goal: GoalOutput | None
    unallocated_savings: int
    candidates: list[GoalCandidate]
    limit: int
    offset: int


class GoalPreviewOutput(BaseModel):
    mode: Literal["FIRST", "CHANGE", "NEXT"]
    current_goal_id: str | None
    starting_saved_amount: int
    display_starting_amount: int
    remaining_amount: int
    progress_percent: float
    achieved: bool
    message: str


class GoalReplacementOutput(BaseModel):
    goal: GoalOutput
    previous_goal_id: str
    carried_amount: int
    retained_amount: int


def unallocated_savings(session, guest_id):
    return int(
        session.scalar(
            select(func.coalesce(func.sum(SavingEvent.amount), 0)).where(
                SavingEvent.guest_id == guest_id, SavingEvent.goal_id.is_(None)
            )
        )
        or 0
    )


@router.get("/goals/setup", response_model=GoalSetupOutput)
def setup(
    session: DB,
    guest: Owner,
    today: Today,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    lock_guest(session, guest.id)
    current = active_goal(session, guest.id)
    if current:
        record_goal_achievement(session, current, today)
        session.commit()
    candidates = session.scalars(
        select(Item)
        .where(Item.guest_id == guest.id)
        .order_by(Item.created_at.desc(), Item.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "mode": "NEXT" if current and current.achieved_on else "CHANGE" if current else "FIRST",
        "current_goal": goal_summary(session, current, today) if current else None,
        "unallocated_savings": 0,
        "candidates": [
            {
                "item_id": item.id,
                "name": item.name,
                "target_amount": item.price,
                "source_url": item.source_url,
                "image_url": item.image_url,
            }
            for item in candidates
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/goals/preview", response_model=GoalPreviewOutput)
def preview(body: GoalPreviewInput, session: DB, guest: Owner, today: Today):
    lock_guest(session, guest.id)
    current = active_goal(session, guest.id)
    if current:
        record_goal_achievement(session, current, today)
        session.commit()
    if body.mode == "FIRST":
        if current is not None or body.current_goal_id is not None:
            raise AppError(409, "ACTIVE_GOAL_EXISTS", "현재 목표 정보를 다시 확인해 주세요.")
        starting = 0
    else:
        if current is None or body.current_goal_id != current.id:
            raise AppError(409, "GOAL_CHANGED", "현재 목표가 바뀌었어요. 설정 화면을 다시 열어 주세요.")
        if body.mode == "NEXT":
            if not current.achieved_on:
                raise AppError(409, "GOAL_NOT_ACHIEVED", "진행 중인 목표는 목표 바꾸기를 이용해 주세요.")
            starting = 0
        else:
            if current.achieved_on:
                raise AppError(409, "GOAL_ALREADY_ACHIEVED", "달성한 목표는 다음 목표로 진행해 주세요.")
            starting = (
                min(max(0, goal_summary(session, current, today)["saved_amount"]), body.target_amount)
                if body.carry_savings
                else 0
            )
    remaining = max(0, body.target_amount - starting)
    return {
        "mode": body.mode,
        "current_goal_id": current.id if current else None,
        "starting_saved_amount": starting,
        "display_starting_amount": min(starting, body.target_amount),
        "remaining_amount": remaining,
        "progress_percent": min(100, round(starting * 100 / body.target_amount, 2)),
        "achieved": remaining == 0,
        "message": f"목표까지 {remaining:,}원 남았어요. 남은 예산으로 달성일을 계산해요."
        if remaining
        else "이미 다 모였어요. 지금 죄책감 없이 살 수 있어요",
    }


@router.post("/goals/current/replacement", response_model=GoalReplacementOutput, status_code=201)
def replace_goal(
    body: GoalReplacementInput, idempotency_key: IdempotencyKey, session: DB, guest: Owner, today: Today
):
    lock_guest(session, guest.id)
    fingerprint = request_hash(body.model_dump())
    existing = session.scalar(
        select(GoalReplacement).where(
            GoalReplacement.guest_id == guest.id, GoalReplacement.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 중복 방지 키에 다른 요청을 사용할 수 없어요.")
        return existing.response
    previous = active_goal(session, guest.id)
    if previous is None or previous.id != body.current_goal_id:
        raise AppError(409, "GOAL_CHANGED", "현재 목표가 바뀌었어요. 설정 화면을 다시 열어 주세요.")
    record_goal_achievement(session, previous, today)
    if previous.achieved_on:
        raise AppError(409, "GOAL_ALREADY_ACHIEVED", "달성한 목표는 다음 목표로 진행해 주세요.")
    previous_plan = goal_funding.plan(session, previous, today) if previous.funding else None
    available = previous_plan["saved"] if previous_plan else 0
    carried = min(available, body.target_amount) if body.carry_savings else 0
    reserved = (
        previous_plan["month_allocated"]
        if previous_plan and previous_plan["allocation_month"] == today.strftime("%Y-%m")
        else 0
    )
    if previous_plan:
        goal_funding.freeze(previous, previous_plan, today, available - carried)
    previous.active = False
    session.flush()
    goal = Goal(
        guest_id=guest.id,
        funding=goal_funding.opening(session, guest.id, today, carried, reserved),
        **body.model_dump(exclude={"current_goal_id", "carry_savings"}),
    )
    session.add(goal)
    session.flush()
    replacement = GoalReplacement(
        guest_id=guest.id,
        from_goal_id=previous.id,
        to_goal_id=goal.id,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        carry_savings=body.carry_savings,
        response={},
    )
    session.add(replacement)
    session.flush()
    record_goal_achievement(session, goal, today)
    response = jsonable_encoder(
        {
            "goal": goal_summary(session, goal, today),
            "previous_goal_id": previous.id,
            "carried_amount": carried,
            "retained_amount": available - carried,
        }
    )
    replacement.response = response
    session.commit()
    return response
