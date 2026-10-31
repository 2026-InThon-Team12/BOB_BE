"""Observed decisions and explicitly estimated usage, never fabricated demo ratios."""

from datetime import date, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import (
    Item,
    Purchase,
    PurchaseReason,
    PurchaseReview,
    SavingEvent,
    PurchaseQuestion,
    PurchaseQuestionAnswer,
)
from app.services.finance import month_days


def item_reasons(item: Item) -> list[str]:
    return item.purchase_reasons or [item.purchase_reason]


def reason_statistics(session: Session, guest_id: str, month: str | None = None) -> list[dict]:
    items = list(session.scalars(select(Item).where(Item.guest_id == guest_id)))
    if month:
        items = [item for item in items if item.decided_on and item.decided_on.strftime("%Y-%m") == month]
    result = []
    for reason in PurchaseReason:
        registered = [item for item in items if reason.value in item_reasons(item)]
        decided = [item for item in registered if item.decision in ("BUY", "GIVE_UP")]
        abandoned = sum(item.decision == "GIVE_UP" for item in decided)
        result.append(
            {
                "reason": reason.value,
                "registered_count": len(registered),
                "decided_count": len(decided),
                "abandoned_count": abandoned,
                "abandonment_rate": abandoned / len(decided) if decided else None,
            }
        )
    return result


def category_statistics(session: Session, guest_id: str, today: date, month: str | None = None) -> list[dict]:
    from app.services.simulations import purchase_view

    weekly = {}
    answers = session.execute(
        select(PurchaseQuestion, PurchaseQuestionAnswer, Item)
        .join(PurchaseQuestionAnswer, PurchaseQuestionAnswer.question_id == PurchaseQuestion.id)
        .join(Purchase, Purchase.id == PurchaseQuestion.purchase_id)
        .join(Item, Item.id == Purchase.item_id)
        .where(
            PurchaseQuestion.guest_id == guest_id,
            PurchaseQuestion.template_code == "USAGE_DAYS",
            PurchaseQuestion.question_date <= today,
        )
    ).all()
    for question, answer, item in answers:
        if month and answer.answered_on.strftime("%Y-%m") != month:
            continue
        actual = next(
            (
                option.get("value")
                for option in question.answer_spec["options"]
                if option["code"] == answer.choice_code
            ),
            None,
        )
        predicted = question.context_snapshot.get("expected_used_days_in_window")
        if actual is None or predicted is None:
            continue
        entry = weekly.setdefault(question.purchase_id, {"category": item.category, "windows": {}})
        window = question.context_snapshot["usage_window"]
        entry["windows"][(window["start"], window["end"])] = (predicted, actual)

    rows = session.execute(
        select(Purchase, PurchaseReview, Item)
        .join(PurchaseReview, PurchaseReview.purchase_id == Purchase.id)
        .join(Item, Item.id == Purchase.item_id)
        .where(Purchase.guest_id == guest_id)
    ).all()
    # A new one-time review supersedes historical weekly answers for that purchase.
    for purchase, review, item in rows:
        if (review.frequency or "").startswith("DAYS_") and (
            not month or review.submitted_on.strftime("%Y-%m") == month
        ):
            weekly.pop(purchase.id, None)

    measurements = []
    for entry in weekly.values():
        predicted = sum(value[0] for value in entry["windows"].values())
        actual = sum(value[1] for value in entry["windows"].values())
        measurements.append(
            (
                entry["category"],
                predicted,
                actual,
                actual,
                actual,
                False,
                len(entry["windows"]),
                len(entry["windows"]) * 7,
                len(entry["windows"]) * 7,
            )
        )

    for purchase, review, item in rows:
        # Avoid counting legacy weekly and legacy survey records twice.
        if purchase.id in weekly:
            continue
        if month and review.submitted_on.strftime("%Y-%m") != month:
            continue
        view = purchase_view(session, purchase, today)
        measurements.append(
            (
                item.category,
                view["expected_days_in_review_window"] if view["comparison_reason"] is None else None,
                view["actual_days_estimate"],
                view["review"]["min_days"],
                view["review"]["max_days"],
                view["is_estimate"],
                1,
                (purchase.review_end - purchase.review_start).days + 1,
                view["prediction_window_days"],
            )
        )
    groups = {}
    for category, predicted, actual, low, high, estimated, count, observed, forecast_window in measurements:
        group = groups.setdefault(
            category,
            {
                "category": category,
                "sample_count": 0,
                "predicted_days": 0.0,
                "actual_days_estimate": 0.0,
                "actual_days_min": 0.0,
                "actual_days_max": 0.0,
                "is_estimate": False,
                "measurement_count": 0,
                "observed_days": 0,
                "prediction_window_days": 0,
                "matching_windows": True,
            },
        )
        group["sample_count"] += 1
        if predicted is None:
            group["predicted_days"] = None
        elif group["predicted_days"] is not None:
            group["predicted_days"] += predicted
        group["actual_days_estimate"] += actual
        group["actual_days_min"] += low
        group["actual_days_max"] += high
        group["is_estimate"] |= estimated
        group["measurement_count"] += count
        group["observed_days"] += observed
        group["prediction_window_days"] += forecast_window
        group["matching_windows"] &= forecast_window == observed
    for group in groups.values():
        actual = group["actual_days_estimate"]
        group["overprediction_ratio_estimate"] = (
            round(group["predicted_days"] / actual, 2)
            if actual and group["predicted_days"] is not None and group["matching_windows"] else None
        )
        group["status"] = (
            "PREDICTION_UNAVAILABLE"
            if group["predicted_days"] is None
            else "DIFFERENT_OBSERVATION_WINDOWS"
            if not group["matching_windows"]
            else "NO_ACTUAL_USE"
            if not actual
            else "OVERPREDICTED"
            if group["predicted_days"] > actual * 1.25
            else "SIMILAR"
            if group["predicted_days"] >= actual * 0.75
            else "UNDERPREDICTED"
        )
        group["metric_unit"] = "usage_days"
    return list(groups.values())


def calibration_for(category: str, statistics: list[dict]) -> dict | None:
    group = next((row for row in statistics if row["category"] == category), None)
    if not group or group["predicted_days"] is None or not group["matching_windows"]:
        return None
    ratio = group["overprediction_ratio_estimate"]
    if (ratio is None and group["predicted_days"] > 0) or (ratio is not None and ratio >= 2):
        return {
            **group,
            "message": "이 분야는 이전 예상보다 실제 사용이 적었어요. 오늘의 구체적인 사용 상황을 떠올려 주세요.",
        }
    return None


def monthly_report(session: Session, guest_id: str, month: str, today: date) -> dict:
    from app.core.clock import SEOUL
    from datetime import datetime, time

    start = date.fromisoformat(month + "-01")
    end = start + timedelta(days=month_days(month))
    registered = session.scalar(
        select(func.count(Item.id)).where(
            Item.guest_id == guest_id,
            Item.created_at >= datetime.combine(start, time.min, tzinfo=SEOUL),
            Item.created_at < datetime.combine(end, time.min, tzinfo=SEOUL),
        )
    )
    bye = session.scalar(
        select(func.count(Item.id)).where(
            Item.guest_id == guest_id,
            Item.decision == "GIVE_UP",
            Item.decided_on >= start,
            Item.decided_on < end,
        )
    )
    saved = session.scalar(
        select(func.coalesce(func.sum(SavingEvent.amount), 0)).where(
            SavingEvent.guest_id == guest_id, SavingEvent.occurred_on >= start, SavingEvent.occurred_on < end
        )
    )
    categories = category_statistics(session, guest_id, today, month)
    strongest = max(
        categories,
        key=lambda value: (
            value["actual_days_estimate"] == 0 and (value["predicted_days"] or 0) > 0,
            value["overprediction_ratio_estimate"] or 0,
        ),
        default=None,
    )
    return {
        "month": month,
        "registered_count": registered,
        "bye_count": bye,
        "saved_amount": int(saved or 0),
        "categories": categories,
        "purchase_reasons": reason_statistics(session, guest_id, month),
        "discovery": calibration_for(strongest["category"], categories) if strongest else None,
        "metric_unit": "usage_days",
        "estimate_method": "range_midpoint"
        if any(row["is_estimate"] for row in categories)
        else "reported_usage_days",
        "empty": not registered and not categories and not bye and not saved,
    }
