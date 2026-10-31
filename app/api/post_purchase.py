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
from app.models import Guest, Purchase, PurchaseQuestion
from app.question_schemas import PurchaseAnswerInput, PurchaseQuestionOutput, PurchaseQuestionHistory
from app.services.purchase_questions import (
    QuestionAI,
    generate_question,
    question_output,
    save_question_answer,
    usage_statistics,
)
from app.services.question_catalog import catalog_output

router = APIRouter(tags=["purchase questions"])
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]


def get_question_ai(settings: Annotated[Settings, Depends(get_settings)]):
    return QuestionAI(settings)


@router.get("/question-templates")
def question_templates():
    return catalog_output()


@router.post("/purchases/{purchase_id}/daily-question", response_model=PurchaseQuestionOutput)
def daily_question(
    purchase_id: str,
    session: DB,
    guest: Owner,
    today: Today,
    ai: Annotated[QuestionAI, Depends(get_question_ai)],
):
    # purchase + Seoul date is the natural idempotency key; callers cannot set dates.
    question = generate_question(session, guest.id, purchase_id, today, ai)
    return question_output(session, question)


@router.get("/purchases/{purchase_id}/questions", response_model=PurchaseQuestionHistory)
def question_history(
    purchase_id: str,
    session: DB,
    guest: Owner,
    today: Today,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    purchase = session.scalar(
        select(Purchase).where(Purchase.id == purchase_id, Purchase.guest_id == guest.id)
    )
    if purchase is None:
        raise AppError(404, "PURCHASE_NOT_FOUND", "구매 기록을 찾을 수 없습니다.")
    rows = session.scalars(
        select(PurchaseQuestion)
        .where(PurchaseQuestion.purchase_id == purchase.id)
        .order_by(PurchaseQuestion.question_date.desc())
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [question_output(session, row) for row in rows],
        "usage_summary": usage_statistics(session, purchase, today),
        "limit": limit,
        "offset": offset,
    }


@router.get("/purchase-questions/{question_id}", response_model=PurchaseQuestionOutput)
def get_question(question_id: str, session: DB, guest: Owner):
    question = session.scalar(
        select(PurchaseQuestion).where(
            PurchaseQuestion.id == question_id, PurchaseQuestion.guest_id == guest.id
        )
    )
    if question is None:
        raise AppError(404, "QUESTION_NOT_FOUND", "질문을 찾을 수 없습니다.")
    return question_output(session, question)


@router.put("/purchase-questions/{question_id}/answer", response_model=PurchaseQuestionOutput)
def answer_question(question_id: str, body: PurchaseAnswerInput, session: DB, guest: Owner, today: Today):
    question = save_question_answer(
        session, guest.id, question_id, today, body.choice_code, body.number_value
    )
    return question_output(session, question)
