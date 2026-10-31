import hashlib
import secrets
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_guest, hash_token
from app.core.clock import get_today, utc_now
from app.core.errors import AppError
from app.db import get_session
from app.models import (
    Category,
    DailyAnswer,
    Device,
    Guest,
    Item,
    Purchase,
    PurchaseReason,
    PurchaseReview,
    RegistrationBatch,
    Simulation,
    SimulationExtension,
    Expense,
    SavingEvent,
)
from app.schemas import (
    AnswerOutput,
    AnswerRequest,
    BatchOutput,
    BatchRequest,
    DecisionRequest,
    GuestSessionOutput,
    ItemListOutput,
    ItemOutput,
    ItemPatch,
    PurchaseOutput,
    PushTokenOutput,
    PushTokenRequest,
    ReviewRequest,
    SimulationResult,
    TodayOutput,
)
from app.services.ai import default_question
from app.services.finance import active_goal, record_goal_achievement
from app.services.registration import preview_registration, scheduled_days
from app.services.simulations import (
    observation_dates,
    REVIEW_OPTIONS,
    duration_for_price,
    get_today_cards,
    item_view,
    purchase_view,
    simulation_result,
)

router = APIRouter()
DB = Annotated[Session, Depends(get_session)]
Owner = Annotated[Guest, Depends(get_guest)]
Today = Annotated[date, Depends(get_today)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def lock_owner(session: Session, guest: Guest) -> None:
    session.execute(select(Guest.id).where(Guest.id == guest.id).with_for_update())


def owned(session: Session, model, object_id: str, guest_id: str):
    value = session.scalar(select(model).where(model.id == object_id, model.guest_id == guest_id))
    if value is None:
        raise AppError(404, "NOT_FOUND", "항목을 찾을 수 없습니다.")
    return value


@router.post("/guest-sessions", status_code=201, tags=["guest"], response_model=GuestSessionOutput)
def create_guest(session: DB):
    token = secrets.token_urlsafe(32)
    guest = Guest(token_hash=hash_token(token))
    session.add(guest)
    session.commit()
    return {"guest_id": guest.id, "access_token": token, "token_type": "bearer"}


@router.put("/me/push-token", tags=["guest"], response_model=PushTokenOutput)
def register_push(body: PushTokenRequest, session: DB, guest: Owner):
    lock_owner(session, guest)
    other = session.scalar(select(Device).where(Device.fcm_token == body.token, Device.guest_id != guest.id))
    if other:
        raise AppError(409, "TOKEN_ALREADY_REGISTERED", "이미 다른 게스트에 등록된 푸시 토큰입니다.")
    device = session.scalar(select(Device).where(Device.guest_id == guest.id))
    if device is None:
        device = Device(guest_id=guest.id, fcm_token=body.token)
        session.add(device)
    device.fcm_token, device.enabled, device.updated_at = body.token, body.enabled, utc_now()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AppError(409, "TOKEN_ALREADY_REGISTERED", "푸시 토큰 등록이 충돌했습니다.") from None
    return {"enabled": device.enabled}


@router.delete("/me/push-token", status_code=204, tags=["guest"])
def disable_push(session: DB, guest: Owner):
    lock_owner(session, guest)
    device = session.scalar(select(Device).where(Device.guest_id == guest.id))
    if device:
        device.enabled = False
        session.commit()
    return Response(status_code=204)


@router.get("/metadata", tags=["items"])
def metadata():
    return {
        "review_options": REVIEW_OPTIONS,
        "metric_unit": "usage_days",
        "categories": dict(
            zip(
                [x.value for x in Category],
                [
                    "일상/생활",
                    "의류",
                    "가구/인테리어",
                    "가전제품",
                    "디지털기기",
                    "취미",
                    "게임",
                    "뷰티/미용",
                    "식품",
                    "기타",
                ],
                strict=True,
            )
        ),
        "purchase_reasons": dict(
            zip(
                [x.value for x in PurchaseReason],
                [
                    "SNS·광고를 보고",
                    "할인·한정 판매 때문에",
                    "일정 금액 이상 담으면 추가 할인을 받을 수 있어서",
                    "스트레스·기분 전환",
                    "주변 사람이 가지고 있어서",
                    "실제 필요해서",
                    "오래전부터 원해서",
                    "기타",
                ],
                strict=True,
            )
        ),
    }


@router.get("/today", tags=["simulation"], response_model=TodayOutput)
def today_cards(session: DB, guest: Owner, today: Today, include_answered: bool = False):
    return get_today_cards(session, guest.id, today, include_answered)


@router.post("/items/batch", status_code=201, tags=["items"], response_model=BatchOutput)
def register_items(
    body: BatchRequest, session: DB, guest: Owner, today: Today, idempotency_key: IdempotencyKey
):
    lock_owner(session, guest)
    request_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    existing = session.scalar(
        select(RegistrationBatch).where(
            RegistrationBatch.guest_id == guest.id,
            RegistrationBatch.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 키를 다른 등록 요청에 사용할 수 없습니다.")
        return existing.response
    preview = preview_registration(body, today)
    if preview["discount_check_required"]:
        raise AppError(409, "DISCOUNT_CHECK_REQUIRED", "시작 전에 할인 조건과 품목 스위치를 확인해 주세요.")
    registered = []
    for index, data in enumerate(body.items):
        if index not in preview["selected_item_indexes"]:
            continue
        item = Item(
            guest_id=guest.id,
            **data.model_dump(exclude={"supplemental_question", "question_mode"}),
        )
        session.add(item)
        session.flush()
        days = scheduled_days(item.price, item.sale_ends_on, today)
        start = today + timedelta(days=1) if days else today
        session.add(
            Simulation(
                guest_id=guest.id,
                item_id=item.id,
                price_snapshot=item.price,
                duration_days=days,
                base_duration_days=days,
                question_version={"LOCAL": "local-v1", "ADAPTIVE": "pre-v1", "LEGACY": "v1"}[
                    data.question_mode
                ],
                start_date=start,
                end_date=start + timedelta(days=days),
                measurement_question=data.supplemental_question or default_question(item.category, item.name),
                supplemental_question=data.supplemental_question
                or default_question(item.category, item.name),
            )
        )
        session.flush()
        registered.append(item_view(session, item, today))
    response = jsonable_encoder({"items": registered, "discount": preview["discount"]})
    session.add(
        RegistrationBatch(
            guest_id=guest.id, idempotency_key=idempotency_key, request_hash=request_hash, response=response
        )
    )
    session.commit()
    return response


@router.post("/registration-previews", tags=["items"])
def registration_preview(body: BatchRequest, guest: Owner, today: Today):
    return preview_registration(body, today)


@router.get("/items", tags=["items"], response_model=ItemListOutput)
def list_items(
    session: DB,
    guest: Owner,
    today: Today,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    items = session.scalars(
        select(Item)
        .where(Item.guest_id == guest.id)
        .order_by(Item.created_at.desc(), Item.id)
        .offset(offset)
        .limit(limit)
    )
    return {"items": [item_view(session, item, today) for item in items], "limit": limit, "offset": offset}


@router.get("/items/{item_id}", tags=["items"], response_model=ItemOutput)
def get_item(item_id: str, session: DB, guest: Owner, today: Today):
    return item_view(session, owned(session, Item, item_id, guest.id), today)


@router.patch("/items/{item_id}", tags=["items"], response_model=ItemOutput)
def patch_item(item_id: str, body: ItemPatch, session: DB, guest: Owner, today: Today):
    lock_owner(session, guest)
    item = owned(session, Item, item_id, guest.id)
    simulation = session.scalar(select(Simulation).where(Simulation.item_id == item.id))
    if today >= simulation.start_date or item.decision == "BUY":
        raise AppError(409, "SIMULATION_FROZEN", "시뮬레이션 시작 후에는 등록 정보를 변경할 수 없습니다.")
    updates = body.model_dump(exclude_unset=True)
    if not updates or any(
        value is None for key, value in updates.items() if key not in {"sale_ends_on", "reason_other_text"}
    ):
        raise AppError(422, "INVALID_PATCH", "변경할 값은 비어 있을 수 없습니다.")
    if "purchase_reasons" in updates:
        if len(set(updates["purchase_reasons"])) != len(updates["purchase_reasons"]):
            raise AppError(422, "DUPLICATE_REASONS", "구매 이유는 중복 선택할 수 없습니다.")
        if "purchase_reason" in updates and updates["purchase_reason"] not in updates["purchase_reasons"]:
            raise AppError(422, "REASONS_CONFLICT", "구매 이유를 확인해 주세요.")
        updates["purchase_reasons"] = sorted(updates["purchase_reasons"])
        updates["purchase_reason"] = updates["purchase_reasons"][0]
    elif "purchase_reason" in updates:
        updates["purchase_reasons"] = [updates["purchase_reason"]]
    reasons = updates.get("purchase_reasons", item.purchase_reasons or [item.purchase_reason])
    if "EXTRA_DISCOUNT" in reasons and "EXTRA_DISCOUNT" not in (
        item.purchase_reasons or [item.purchase_reason]
    ):
        raise AppError(
            409, "DISCOUNT_CHECK_REQUIRED", "추가 할인 품목은 장바구니 할인 확인 후 새로 등록해 주세요."
        )
    other = updates.get("reason_other_text", item.reason_other_text)
    sale = updates.get("sale_ends_on", item.sale_ends_on)
    if ("OTHER" in reasons) != (other is not None) or ("DISCOUNT_LIMITED" in reasons) != (sale is not None):
        raise AppError(422, "REASON_DETAIL_REQUIRED", "구매 이유에 맞는 기타 내용과 마감일을 확인해 주세요.")
    for key, value in updates.items():
        if key != "supplemental_question":
            setattr(item, key, value)
    simulation.price_snapshot = item.price
    simulation.duration_days = scheduled_days(item.price, item.sale_ends_on, today)
    simulation.start_date = today + timedelta(days=1) if simulation.duration_days else today
    simulation.base_duration_days = simulation.duration_days
    simulation.end_date = simulation.start_date + timedelta(days=simulation.duration_days)
    if "supplemental_question" in updates:
        simulation.supplemental_question = updates["supplemental_question"]
    elif "name" in updates or "category" in updates:
        simulation.supplemental_question = default_question(item.category, item.name)
    simulation.measurement_question = simulation.supplemental_question
    session.commit()
    return item_view(session, item, today)


@router.put(
    "/simulations/{simulation_id}/answers/{answer_date}", tags=["simulation"], response_model=AnswerOutput
)
def save_answer(
    simulation_id: str, answer_date: date, body: AnswerRequest, session: DB, guest: Owner, today: Today
):
    lock_owner(session, guest)
    simulation = owned(session, Simulation, simulation_id, guest.id)
    if simulation.question_version == "local-v1":
        raise AppError(
            409, "LOCAL_STORAGE_REQUIRED", "질문 응답은 기기에 저장하고 결과 요약 때 제출해 주세요."
        )
    if simulation.question_version == "pre-v1":
        raise AppError(409, "ADAPTIVE_QUESTION_REQUIRED", "생성된 구매 전 질문 ID로 답변해 주세요.")
    if answer_date != today:
        raise AppError(409, "ANSWER_DATE_INVALID", "오늘 날짜의 답변만 작성하거나 수정할 수 있습니다.")
    if simulation.ended_early_on or today not in observation_dates(session, simulation):
        raise AppError(409, "SIMULATION_NOT_ACTIVE", "진행 중인 시뮬레이션만 응답할 수 있습니다.")
    answer = session.scalar(
        select(DailyAnswer).where(
            DailyAnswer.simulation_id == simulation.id,
            DailyAnswer.answer_date == today,
        )
    )
    if answer is None:
        answer = DailyAnswer(simulation_id=simulation.id, answer_date=today, **body.model_dump())
        session.add(answer)
    else:
        answer.usage_answer, answer.supplemental_answer = body.usage_answer, body.supplemental_answer
    session.commit()
    return {"answer_date": today, **body.model_dump()}


@router.get("/simulations/{simulation_id}/result", tags=["simulation"], response_model=SimulationResult)
def result(simulation_id: str, session: DB, guest: Owner, today: Today):
    return simulation_result(session, owned(session, Simulation, simulation_id, guest.id), today)


@router.put("/items/{item_id}/decision", tags=["purchase"], response_model=ItemOutput)
def decide(
    item_id: str,
    body: DecisionRequest,
    session: DB,
    guest: Owner,
    today: Today,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ] = None,
):
    lock_owner(session, guest)
    item = owned(session, Item, item_id, guest.id)
    simulation = session.scalar(select(Simulation).where(Simulation.item_id == item.id))
    if body.decision == "HOLD" and idempotency_key:
        previous = session.scalar(
            select(SimulationExtension).where(
                SimulationExtension.simulation_id == simulation.id,
                SimulationExtension.idempotency_key == idempotency_key,
            )
        )
        if previous:
            return item_view(session, item, today)
    if body.decision != "BUY" and (body.purchased_on is not None or body.actual_price is not None):
        raise AppError(422, "INVALID_DECISION", "구매 정보는 구매 결정에만 입력합니다.")
    if item.decision in ("BUY", "GIVE_UP"):
        if item.decision != body.decision:
            raise AppError(409, "DECISION_FINAL", "이미 확정된 결정입니다.")
        if item.decision == "BUY":
            purchase = session.scalar(select(Purchase).where(Purchase.item_id == item.id))
            if (body.purchased_on is not None and body.purchased_on != purchase.purchased_on) or (
                body.actual_price is not None and body.actual_price != purchase.actual_price
            ):
                raise AppError(409, "DECISION_FINAL", "확정한 구매 정보를 변경할 수 없습니다.")
        return item_view(session, item, today)
    if body.decision != "BUY" and today < simulation.end_date:
        raise AppError(409, "SIMULATION_NOT_COMPLETE", "시뮬레이션 종료 후 보류하거나 포기할 수 있습니다.")
    if body.decision == "HOLD":
        if not idempotency_key:
            raise AppError(422, "IDEMPOTENCY_KEY_REQUIRED", "7일 연장 요청에는 Idempotency-Key가 필요합니다.")
        start = today + timedelta(days=1)
        extension_days = min(7, (item.sale_ends_on - today).days) if item.sale_ends_on else 7
        if extension_days <= 0:
            raise AppError(409, "SALE_PERIOD_ENDED", "할인 마감일을 지나 질문 기간을 연장할 수 없습니다.")
        session.add(
            SimulationExtension(
                simulation_id=simulation.id,
                idempotency_key=idempotency_key,
                start_date=start,
                end_date=start + timedelta(days=extension_days),
            )
        )
        if simulation.base_duration_days is None:
            simulation.base_duration_days = duration_for_price(simulation.price_snapshot)
        simulation.duration_days += extension_days
        simulation.end_date, simulation.resume_on = start + timedelta(days=extension_days), start
        simulation.local_result = None
    elif body.decision == "GIVE_UP":
        goal = active_goal(session, guest.id)
        session.add(
            SavingEvent(
                guest_id=guest.id,
                goal_id=goal.id if goal else None,
                item_id=item.id,
                kind="CREDIT",
                amount=item.price,
                occurred_on=today,
            )
        )
    elif body.decision == "BUY":
        purchased_on = body.purchased_on or today
        # Backdated purchases must remain within this item's registration history.
        if purchased_on > today or purchased_on < simulation.start_date - timedelta(days=1):
            raise AppError(422, "PURCHASE_DATE_INVALID", "등록일 이후, 오늘까지의 구매일을 입력해 주세요.")
        if purchased_on < simulation.end_date:
            simulation.ended_early_on = purchased_on
        from app.services.pre_purchase_questions import baseline, is_adaptive

        before_purchase = baseline(session, simulation, purchased_on) if is_adaptive(simulation) else None
        due = purchased_on + timedelta(days=14)
        session.add(
            Purchase(
                guest_id=guest.id,
                item_id=item.id,
                purchased_on=purchased_on,
                actual_price=body.actual_price or item.price,
                review_due_on=due,
                review_start=purchased_on,
                review_end=due - timedelta(days=1),
                pre_purchase_snapshot=before_purchase,
            )
        )
        session.flush()
        purchase = session.scalar(select(Purchase).where(Purchase.item_id == item.id))
        session.add(
            Expense(
                guest_id=guest.id,
                source="PURCHASE",
                purchase_id=purchase.id,
                name=item.name,
                amount=purchase.actual_price,
                spent_on=purchased_on,
            )
        )
    item.decision = body.decision
    item.decided_on = today
    if body.decision == "GIVE_UP":
        record_goal_achievement(session, goal, today)
    session.commit()
    return item_view(session, item, today)


@router.put("/purchases/{purchase_id}/review", tags=["purchase"], response_model=PurchaseOutput)
def review_purchase(purchase_id: str, body: ReviewRequest, session: DB, guest: Owner, today: Today):
    lock_owner(session, guest)
    purchase = owned(session, Purchase, purchase_id, guest.id)
    if today < purchase.review_due_on:
        raise AppError(409, "REVIEW_NOT_DUE", "아직 구매 후 점검일이 되지 않았습니다.")
    window = (purchase.review_end - purchase.review_start).days + 1
    if body.frequency and window != 14:
        raise AppError(409, "LEGACY_REVIEW_WINDOW", "기존 7일 회고는 사용일수를 직접 수정해 주세요.")
    if body.used_days is not None and body.used_days > window:
        raise AppError(422, "USAGE_EXCEEDS_WINDOW", "회고 기간보다 사용일수가 많을 수 없습니다.")
    review = session.scalar(select(PurchaseReview).where(PurchaseReview.purchase_id == purchase.id))
    if review is None:
        review = PurchaseReview(
            purchase_id=purchase.id, used_days=body.used_days, frequency=body.frequency, submitted_on=today
        )
        session.add(review)
    else:
        review.used_days, review.frequency, review.submitted_on = body.used_days, body.frequency, today
    session.commit()
    return purchase_view(session, purchase, today)
