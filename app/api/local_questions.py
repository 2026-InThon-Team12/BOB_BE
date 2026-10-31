from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.pre_purchase import get_pre_question_ai
from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db import get_session
from app.models import Guest, Item, Purchase, Simulation
from app.services.finance import lock_guest
from app.services.local_questions import (
    LocalQuestionRequest,
    LocalSummaryRequest,
    aggregate_responses,
    local_post_question,
    local_pre_question,
    local_result,
    owned_local,
    summarize_local,
)
from app.services.pre_purchase_questions import PreQuestionAI
from app.services.purchase_questions import QuestionAI

router = APIRouter(tags=["local question storage"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]
Config = Annotated[Settings, Depends(get_settings)]


@router.post("/simulations/{simulation_id}/local-question")
def generate_pre(
    simulation_id: str,
    body: LocalQuestionRequest,
    session: DB,
    guest: Owner,
    today: Today,
    ai: Annotated[PreQuestionAI, Depends(get_pre_question_ai)],
):
    simulation = owned_local(session, guest.id, simulation_id)
    return local_pre_question(session, simulation, today, body, ai)


@router.post("/simulations/{simulation_id}/result-summary")
def summarize(
    simulation_id: str, body: LocalSummaryRequest, session: DB, guest: Owner, today: Today, settings: Config
):
    lock_guest(session, guest.id)
    simulation = owned_local(session, guest.id, simulation_id)
    item = session.get(Item, simulation.item_id)
    if item.decision in ("BUY", "GIVE_UP"):
        raise AppError(409, "DECISION_FINAL", "결정 후에는 예측 집계를 변경할 수 없습니다.")
    if today < simulation.end_date:
        raise AppError(409, "SIMULATION_NOT_COMPLETE", "기간 종료 후 로컬 기록을 요약해 주세요.")
    simulation.local_result = aggregate_responses(session, simulation, today, body)
    result = local_result(session, simulation, today)
    session.commit()
    # The transcript and model prose are deliberately not persisted or logged.
    return {
        "result": result,
        "ai_summary": summarize_local(body, result, settings),
        "raw_responses_stored": False,
    }


@router.post("/purchases/{purchase_id}/local-question")
def generate_post(
    purchase_id: str, body: LocalQuestionRequest, session: DB, guest: Owner, today: Today, settings: Config
):
    purchase = session.scalar(
        select(Purchase).where(Purchase.id == purchase_id, Purchase.guest_id == guest.id)
    )
    if purchase is None:
        raise AppError(404, "NOT_FOUND", "구매 기록을 찾을 수 없습니다.")
    simulation = session.scalar(select(Simulation).where(Simulation.item_id == purchase.item_id))
    owned_local(session, guest.id, simulation.id)
    return local_post_question(session, purchase, today, body, QuestionAI(settings))
