"""Achieved goals are displayed immediately, without inferring a purchase."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.errors import AppError
from app.db import get_session
from app.models import Goal, Guest
from app.services.finance import active_goal, goal_summary, lock_guest, record_goal_achievement

router = APIRouter(tags=["goal shelf"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]


def shelf_entry(goal):
    return {
        **(goal.shelf_snapshot or {}),
        "id": goal.id,
        "name": goal.name,
        "image_url": goal.image_url,
        "source_url": goal.source_url,
        "can_shop": goal.source_url is not None,
        "target_amount": goal.target_amount,
        "achieved_on": goal.achieved_on,
    }


@router.get("/goals/shelf")
def shelf(session: DB, guest: Owner, today: Today):
    lock_guest(session, guest.id)
    record_goal_achievement(session, active_goal(session, guest.id), today)
    session.commit()
    goals = list(
        session.scalars(
            select(Goal)
            .where(Goal.guest_id == guest.id, Goal.achieved_on.is_not(None))
            .order_by(Goal.achieved_on.desc(), Goal.id)
        )
    )
    current = active_goal(session, guest.id)
    return {
        "items": [shelf_entry(goal) for goal in goals],
        "count": len(goals),
        "total_target_amount": sum(goal.target_amount for goal in goals),
        "current_goal": goal_summary(session, current, today)
        if current and current.achieved_on is None
        else None,
    }


@router.put("/goals/{goal_id}/purchase", deprecated=True)
def confirm_purchase_removed(goal_id: str, session: DB, guest: Owner):
    if session.scalar(select(Goal.id).where(Goal.id == goal_id, Goal.guest_id == guest.id)) is None:
        raise AppError(404, "NOT_FOUND", "목표를 찾을 수 없습니다.")
    raise AppError(
        410,
        "GOAL_PURCHASE_CONFIRMATION_REMOVED",
        "목표 달성 시 자동으로 진열됩니다. 구매 확인은 필요하지 않습니다.",
    )
