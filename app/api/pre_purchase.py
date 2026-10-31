from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.auth import get_guest
from app.core.clock import get_today
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db import get_session
from app.models import Guest, Simulation, SimulationQuestion
from app.question_schemas import SimulationAnswerInput, SimulationQuestionOutput, SimulationQuestionHistory
from app.services.pre_purchase_questions import (
    PreQuestionAI,
    baseline,
    generate_question,
    question_output,
    save_answer,
)
from app.services.pre_question_catalog import catalog_output

router = APIRouter(tags=["pre-purchase questions"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]


def get_pre_question_ai(settings: Annotated[Settings, Depends(get_settings)]):
    return PreQuestionAI(settings)


@router.get("/pre-purchase-question-templates")
def question_templates():
    return catalog_output()


@router.post("/simulations/{simulation_id}/daily-question", response_model=SimulationQuestionOutput)
def daily_question(
    simulation_id: str,
    session: DB,
    guest: Owner,
    today: Today,
    ai: Annotated[PreQuestionAI, Depends(get_pre_question_ai)],
):
    return question_output(session, generate_question(session, guest.id, simulation_id, today, ai))


@router.get("/simulations/{simulation_id}/questions", response_model=SimulationQuestionHistory)
def history(
    simulation_id: str,
    session: DB,
    guest: Owner,
    today: Today,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    simulation = session.scalar(
        select(Simulation).where(Simulation.id == simulation_id, Simulation.guest_id == guest.id)
    )
    if simulation is None:
        raise AppError(404, "SIMULATION_NOT_FOUND", "시뮬레이션을 찾을 수 없습니다.")
    rows = session.scalars(
        select(SimulationQuestion)
        .where(SimulationQuestion.simulation_id == simulation.id)
        .order_by(SimulationQuestion.question_date.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [question_output(session, row) for row in rows],
        "baseline": baseline(session, simulation, today),
        "limit": limit,
        "offset": offset,
    }


@router.get("/simulation-questions/{question_id}", response_model=SimulationQuestionOutput)
def get_question(question_id: str, session: DB, guest: Owner):
    question = session.scalar(
        select(SimulationQuestion).where(
            SimulationQuestion.id == question_id, SimulationQuestion.guest_id == guest.id
        )
    )
    if question is None:
        raise AppError(404, "QUESTION_NOT_FOUND", "질문을 찾을 수 없습니다.")
    return question_output(session, question)


@router.put("/simulation-questions/{question_id}/answer", response_model=SimulationQuestionOutput)
def answer_question(question_id: str, body: SimulationAnswerInput, session: DB, guest: Owner, today: Today):
    return question_output(session, save_answer(session, guest.id, question_id, today, body.model_dump()))
