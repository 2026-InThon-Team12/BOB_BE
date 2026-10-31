from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyAnswer, Item, Purchase, PurchaseReview, Simulation, SimulationExtension
from app.core.clock import SEOUL
from app.services.finance import compare_wallet
from app.services.reports import item_reasons, reason_statistics

MEASUREMENT_QUESTION = "오늘 이 물건을 사용할 구체적인 상황이 있었나요?"
ANSWER_OPTIONS = [
    {"value": "YES", "label": "있었어요"},
    {"value": "NO", "label": "딱히 없었어요"},
    {"value": "UNSURE", "label": "잘 모르겠어요"},
]


def duration_for_price(price: int) -> int:
    if price <= 30_000:
        return 3
    return 7 if price < 100_000 else 14


REVIEW_OPTIONS = [
    {"value": "ALMOST_DAILY", "label": "거의 매일", "min_days": 10, "max_days": 14},
    {"value": "OFTEN", "label": "자주", "min_days": 5, "max_days": 9},
    {"value": "SOMETIMES", "label": "가끔", "min_days": 1, "max_days": 4},
    {"value": "NEVER", "label": "한 번도 안 썼어요", "min_days": 0, "max_days": 0},
]


def observation_dates(session: Session, simulation: Simulation) -> list[date]:
    base = (
        simulation.base_duration_days
        if simulation.base_duration_days is not None
        else duration_for_price(simulation.price_snapshot)
    )
    dates = [simulation.start_date + timedelta(days=i) for i in range(base)]
    for extension in session.scalars(
        select(SimulationExtension).where(SimulationExtension.simulation_id == simulation.id)
    ):
        dates.extend(
            extension.start_date + timedelta(days=i)
            for i in range((extension.end_date - extension.start_date).days)
        )
    return sorted(set(dates))


def simulation_status(simulation: Simulation, today: date) -> str:
    if simulation.ended_early_on is not None:
        return "EARLY_PURCHASED"
    if today < simulation.start_date:
        return "SCHEDULED"
    if simulation.resume_on and today < simulation.resume_on:
        return "ON_HOLD"
    return "ACTIVE" if today < simulation.end_date else "COMPLETED"


def simulation_result(session: Session, simulation: Simulation, today: date) -> dict:
    if simulation.question_version == "local-v1":
        from app.services.local_questions import local_result

        return local_result(session, simulation, today)
    from app.services.pre_purchase_questions import adaptive_result, is_adaptive

    if is_adaptive(simulation):
        return adaptive_result(session, simulation, today)
    last_day = min(today, simulation.end_date - timedelta(days=1))
    if simulation.ended_early_on is not None:
        last_day = min(last_day, simulation.ended_early_on)
    observed = [day for day in observation_dates(session, simulation) if day <= last_day]
    observed_days = len(observed)
    answers = list(
        session.scalars(
            select(DailyAnswer).where(
                DailyAnswer.simulation_id == simulation.id,
                DailyAnswer.answer_date.in_(observed),
            )
        )
    )
    yes = sum(answer.usage_answer == "YES" for answer in answers)
    no = sum(answer.usage_answer == "NO" for answer in answers)
    unsure = sum(answer.usage_answer == "UNSURE" for answer in answers)
    definite = yes + no
    status = simulation_status(simulation, today)
    enough = definite * 10 >= simulation.duration_days * 7
    comparable = status == "COMPLETED" and enough
    reason = (
        None
        if comparable
        else (
            "EARLY_PURCHASE"
            if status == "EARLY_PURCHASED"
            else "SIMULATION_NOT_COMPLETE"
            if status != "COMPLETED"
            else "INSUFFICIENT_ANSWERS"
        )
    )
    item = session.get(Item, simulation.item_id)
    reasons = item_reasons(item)
    history = [row for row in reason_statistics(session, simulation.guest_id) if row["reason"] in reasons]
    timeline = [
        {
            "date": day,
            "answer": next((answer.usage_answer for answer in answers if answer.answer_date == day), None),
        }
        for day in observed
    ]
    summary = f"관찰 {observed_days}일 중 사용 상황이 있었던 날은 {yes}일, 없었던 날은 {no}일, 모름은 {unsure}일이에요."
    if yes:
        yes_dates = [answer.answer_date for answer in answers if answer.usage_answer == "YES"]
        if max(yes_dates) <= simulation.start_date + timedelta(days=3) and observed_days > 4:
            summary += " 사용 상황이 있었던 날이 처음 4일에 모여 있어요."
    return {
        "answer_timeline": timeline,
        "response_summary": summary,
        "purchase_reasons": reasons,
        "reason_history": history,
        "metric_unit": "usage_days",
        "simulation_id": simulation.id,
        "status": status,
        "start_date": simulation.start_date,
        "end_date_exclusive": simulation.end_date,
        "duration_days": simulation.duration_days,
        "observed_days": observed_days,
        "answered_days": len(answers),
        "expected_used_days": yes,
        "unused_days": no,
        "unsure_days": unsure,
        "unanswered_days": max(0, observed_days - len(answers)),
        "pending_days": 0 if simulation.ended_early_on else simulation.duration_days - observed_days,
        "definite_answer_days": definite,
        "answer_coverage": definite / simulation.duration_days if simulation.duration_days else 0,
        "expected_usage_rate": yes / definite if definite else None,
        "expected_cost_per_used_day": (simulation.price_snapshot + yes - 1) // yes if yes else None,
        "price_snapshot": simulation.price_snapshot,
        "comparison_eligible": comparable,
        "comparison_reason": reason,
    }


def purchase_view(session: Session, purchase: Purchase, today: date) -> dict:
    review = session.scalar(select(PurchaseReview).where(PurchaseReview.purchase_id == purchase.id))
    simulation = session.scalar(select(Simulation).where(Simulation.item_id == purchase.item_id))
    item = session.get(Item, purchase.item_id)
    result = simulation_result(session, simulation, today)
    window = (purchase.review_end - purchase.review_start).days + 1
    estimate, low, high = None, None, None
    if review:
        if review.frequency:
            option = next(option for option in REVIEW_OPTIONS if option["value"] == review.frequency)
            low, high = option["min_days"], option["max_days"]
            estimate = (low + high) / 2
        else:
            low = high = estimate = review.used_days
    actual_rate = estimate / window if estimate is not None else None
    expected = result["expected_usage_rate"] * window if result["expected_usage_rate"] is not None else None
    eligible = result["comparison_eligible"] and review is not None
    difference = round((result["expected_usage_rate"] - actual_rate) * 100, 2) if eligible else None
    ratio = round(expected / estimate, 2) if eligible and estimate else None
    verdict = None
    if review:
        verdict = "응답이 부족해 수치 비교는 어려워요. 실제 사용 기록은 저장했어요."
        if eligible:
            verdict = (
                "샀지만 아직 사용하지 않았어요."
                if estimate == 0
                else "예상보다 실제 사용이 적었어요."
                if expected > high
                else "예상보다 실제 사용이 많았어요."
                if expected < low
                else "예상 사용량이 실제 사용 구간에 들어가요."
            )
    return {
        "id": purchase.id,
        "item_id": purchase.item_id,
        "item_name": item.name,
        "category": item.category,
        "purchased_on": purchase.purchased_on,
        "actual_price": purchase.actual_price,
        "review_due_on": purchase.review_due_on,
        "review_start": purchase.review_start,
        "review_end": purchase.review_end,
        "review": None
        if review is None
        else {
            "used_days": review.used_days,
            "frequency": review.frequency,
            "min_days": low,
            "max_days": high,
            "estimated_days": estimate,
            "submitted_on": review.submitted_on,
            "late_days": max(0, (review.submitted_on - purchase.review_due_on).days),
        },
        "actual_usage_rate": actual_rate,
        "expected_usage_rate": result["expected_usage_rate"],
        "difference_percentage_points": difference,
        "comparison_reason": result["comparison_reason"] if review else "REVIEW_REQUIRED",
        "review_options": REVIEW_OPTIONS if window == 14 else [],
        "expected_days_in_review_window": expected,
        "actual_days_estimate": estimate,
        "overprediction_ratio_estimate": ratio,
        "is_estimate": bool(review and review.frequency),
        "estimate_method": "range_midpoint" if review and review.frequency else "exact",
        "verdict": verdict,
        "pre_purchase_baseline": purchase.pre_purchase_snapshot,
    }


def item_view(session: Session, item: Item, today: date) -> dict:
    simulation = session.scalar(select(Simulation).where(Simulation.item_id == item.id))
    purchase = session.scalar(select(Purchase).where(Purchase.item_id == item.id))
    return {
        "id": item.id,
        "name": item.name,
        "price": item.price,
        "category": item.category,
        "purchase_reason": item.purchase_reason,
        "purchase_reasons": item_reasons(item),
        "reason_other_text": item.reason_other_text,
        "sale_ends_on": item.sale_ends_on,
        "image_url": item.image_url,
        "source_url": item.source_url,
        "wallet_comparison": compare_wallet(session, item.guest_id, today, item.price),
        "source": item.source,
        "decision": item.decision,
        "simulation": {
            "question_mode": {"pre-v1": "ADAPTIVE", "local-v1": "LOCAL"}.get(
                simulation.question_version, "LEGACY"
            ),
            "current_day": sum(day <= today for day in observation_dates(session, simulation)),
            "question": "" if simulation.question_version == "pre-v1" else simulation.measurement_question,
            "id": simulation.id,
            "status": simulation_status(simulation, today),
            "start_date": simulation.start_date,
            "end_date_exclusive": simulation.end_date,
            "duration_days": simulation.duration_days,
            "observation_dates": observation_dates(session, simulation),
            "measurement_question": simulation.measurement_question,
            "supplemental_question": simulation.supplemental_question,
            "answer_options": ANSWER_OPTIONS,
        },
        "purchase": purchase_view(session, purchase, today) if purchase else None,
    }


def get_today_cards(session: Session, guest_id: str, today: date, include_answered: bool = False) -> dict:
    from app.services.reports import category_statistics, calibration_for

    questions, decisions, reviews, answered_cards = [], [], [], []
    stats = category_statistics(session, guest_id, today)
    rows = session.execute(
        select(Item, Simulation)
        .join(Simulation, Simulation.item_id == Item.id)
        .where(Item.guest_id == guest_id)
        .order_by(Item.created_at, Item.id)
    ).all()
    for item, simulation in rows:
        status = simulation_status(simulation, today)
        if (
            status == "ACTIVE"
            and simulation.question_version not in ("pre-v1", "local-v1")
            and item.decision not in ("BUY", "GIVE_UP")
            and today in observation_dates(session, simulation)
        ):
            answered = session.scalar(
                select(DailyAnswer).where(
                    DailyAnswer.simulation_id == simulation.id, DailyAnswer.answer_date == today
                )
            )
            calibration = calibration_for(item.category, stats)
            card = {
                "item_id": item.id,
                "simulation_id": simulation.id,
                "name": item.name,
                "answer_date": today,
                "measurement_question": simulation.measurement_question,
                "supplemental_question": simulation.supplemental_question,
                "answer_options": ANSWER_OPTIONS,
                "question": simulation.measurement_question,
                "category": item.category,
                "current_day": sum(day <= today for day in observation_dates(session, simulation)),
                "duration_days": simulation.duration_days,
                "calibration": calibration,
                "answer": answered.usage_answer if answered else None,
            }
            if calibration:
                card["question"] += " 실제로 사용할 구체적인 일정이나 상황이 있었는지 한 번 더 떠올려 주세요."
            if answered:
                answered_cards.append(card)
            if not answered or include_answered:
                questions.append(card)
        if status == "COMPLETED" and item.decision in (None, "HOLD"):
            decisions.append(
                {
                    "item_id": item.id,
                    "name": item.name,
                    "decision": item.decision,
                    "result": simulation_result(session, simulation, today),
                }
            )
    for purchase in session.scalars(
        select(Purchase)
        .where(Purchase.guest_id == guest_id, Purchase.review_due_on <= today)
        .order_by(Purchase.review_due_on)
    ):
        if not session.scalar(select(PurchaseReview.id).where(PurchaseReview.purchase_id == purchase.id)):
            reviews.append(purchase_view(session, purchase, today))
    counts = {
        value: sum(card["answer"] == value for card in answered_cards) for value in ("YES", "NO", "UNSURE")
    }
    from app.services.purchase_questions import daily_purchase_tasks

    purchase_tasks, purchase_answered = daily_purchase_tasks(session, guest_id, today, include_answered)
    from app.services.pre_purchase_questions import daily_tasks

    pre_tasks, pre_answered = daily_tasks(session, guest_id, today, include_answered)
    from app.services.local_questions import local_tasks

    pre_tasks += local_tasks(session, guest_id, today)
    return {
        "pre_purchase_questions": pre_tasks,
        "pre_purchase_answered": pre_answered,
        "purchase_questions": purchase_tasks,
        "purchase_answered": purchase_answered,
        "date": today,
        "questions": questions,
        "decisions": decisions,
        "reviews": reviews,
        "answered": answered_cards,
        "summary": {
            "answered_count": len(answered_cards) + len(pre_answered),
            "remaining_count": sum(card["answer"] is None for card in questions)
            + sum(not task["question"] or task["question"]["answer"] is None for task in pre_tasks),
            "pre_purchase_answered_count": len(pre_answered),
            "purchase_answered_count": len(purchase_answered),
            "purchase_remaining_count": sum(
                not task["question"] or task["question"]["answer"] is None for task in purchase_tasks
            ),
            "counts": counts,
        },
        "next_notification_at": datetime.combine(
            today + timedelta(days=1), time(20), tzinfo=SEOUL
        ).isoformat(),
    }
