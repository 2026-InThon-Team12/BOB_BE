"""Pre-purchase reflection; only explicit usage forecasts become usage metrics."""

import copy
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Budget, Item, Simulation, SimulationQuestion, SimulationQuestionAnswer
from app.services.finance import active_goal, compare_spending, lock_guest
from app.services.pre_question_catalog import BY_CODE, CATALOG, VERSION
from app.services.purchase_questions import QuestionAI


class PreQuestionAI(QuestionAI):
    phase = "pre-purchase"
    extra_rules = (
        " This item has NOT been bought. Keep hypothetical tense. EXPECTED_USE_DAYS must ask "
        "산 뒤 30일 동안 며칠; it is an explicit forecast, not observed use. "
        "FIRST_USE asks both when and where/context. PROBLEM_TO_SOLVE asks for one sentence. "
        "Do not infer motives or unnecessary spending from mood, showing others or a discount. "
        "Do not put invented wages, work hours or goal delays in the question; the server supplies these."
    )


def is_adaptive(simulation: Simulation) -> bool:
    return simulation.question_version == VERSION


def answer_value(answer: SimulationQuestionAnswer) -> dict:
    return {
        "choice_code": answer.choice_code,
        "number_value": answer.number_value,
        "text_value": answer.text_value,
        "object_value": answer.object_value,
        "answered_on": answer.answered_on.isoformat(),
    }


def baseline(session: Session, simulation: Simulation, cutoff: date | None = None) -> dict:
    query = (
        select(SimulationQuestion, SimulationQuestionAnswer)
        .join(SimulationQuestionAnswer, SimulationQuestionAnswer.question_id == SimulationQuestion.id)
        .where(SimulationQuestion.simulation_id == simulation.id)
        .order_by(SimulationQuestion.question_date)
    )
    if cutoff:
        query = query.where(SimulationQuestion.question_date <= cutoff)
    latest = {q.template_code: (q, a) for q, a in session.execute(query)}
    result = {
        "source": "PRE_QUESTIONNAIRE",
        "version": VERSION,
        "prediction_window_days": 30,
        "forecast": None,
        "first_use": None,
        "problem_to_solve": None,
        "answers": {},
    }
    for code, (question, answer) in latest.items():
        result["answers"][code] = {
            "question_id": question.id,
            "question_date": question.question_date.isoformat(),
            "question": question.question_text,
            "answer": answer_value(answer),
        }
        if code == "EXPECTED_USE_DAYS" and answer.number_value is not None:
            result["forecast"] = {
                "question_id": question.id,
                "expected_used_days": answer.number_value,
                "window_days": 30,
                "answered_on": answer.answered_on.isoformat(),
            }
        elif code == "FIRST_USE" and answer.object_value:
            result["first_use"] = copy.deepcopy(answer.object_value)
        elif code == "PROBLEM_TO_SOLVE":
            result["problem_to_solve"] = answer.text_value
    return result


def ensure_active(session: Session, simulation: Simulation, today: date):
    from app.services.simulations import observation_dates

    item = session.get(Item, simulation.item_id)
    if not is_adaptive(simulation):
        raise AppError(409, "LEGACY_SIMULATION", "이전 시뮬레이션은 기존 답변 API를 사용해 주세요.")
    if (
        item.decision in ("BUY", "GIVE_UP")
        or simulation.ended_early_on
        or today not in observation_dates(session, simulation)
    ):
        raise AppError(
            409, "SIMULATION_NOT_ACTIVE", "진행 중인 시뮬레이션에서만 오늘 질문에 답할 수 있습니다."
        )


def context_for(session: Session, simulation: Simulation, today: date) -> dict:
    from app.services.simulations import observation_dates

    item = session.get(Item, simulation.item_id)
    month = today.strftime("%Y-%m")
    budget = session.scalar(
        select(Budget).where(Budget.guest_id == simulation.guest_id, Budget.month == month)
    )
    goal = active_goal(session, simulation.guest_id)
    comparison = compare_spending(
        simulation.price_snapshot,
        month,
        income=budget.income if budget else None,
        savings=budget.savings if budget else 0,
    )
    dates = observation_dates(session, simulation)
    return {
        "item_name": item.name,
        "category": item.category,
        "price": simulation.price_snapshot,
        "purchase_reasons": item.purchase_reasons or [item.purchase_reason],
        "question_date": today.isoformat(),
        "observation_day": dates.index(today) + 1,
        "duration_days": simulation.duration_days,
        "is_last_day": today == dates[-1],
        "hourly_wage": budget.hourly_wage if budget else None,
        "work_hours_estimate": round(simulation.price_snapshot / budget.hourly_wage, 2)
        if budget and budget.hourly_wage
        else None,
        "goal": {"id": goal.id, "name": goal.name} if goal else None,
        "goal_delay_days": comparison.delay_days if goal else None,
        "previous_answers": baseline(session, simulation, today),
    }


def candidates_for(session: Session, simulation: Simulation, context: dict):
    if context["is_last_day"] and context["previous_answers"]["forecast"] is None:
        return [BY_CODE["EXPECTED_USE_DAYS"]]
    history = list(
        session.scalars(
            select(SimulationQuestion)
            .where(SimulationQuestion.simulation_id == simulation.id)
            .order_by(SimulationQuestion.question_date.desc())
        )
    )
    eligible = [
        row
        for row in CATALOG
        if not (row.code == "WORK_VALUE" and context["work_hours_estimate"] is None)
        and not (row.code == "GOAL_DELAY" and context["goal_delay_days"] is None)
    ]
    stages = ("TRIGGER", "NECESSITY", "PREDICTION", "VALUE", "PERSISTENCE")
    stage = stages[(context["observation_day"] - 1) % len(stages)]
    candidates = [row for row in eligible if row.stage == stage]
    recent = {row.template_code for row in history[:2]}
    candidates = [row for row in candidates if row.code not in recent] or candidates
    candidates.sort(key=lambda row: (sum(old.template_code == row.code for old in history), row.code))
    return candidates


def reference_text(code: str, context: dict):
    if code == "WORK_VALUE":
        return f"입력 시급 {context['hourly_wage']:,}원 기준 약 {context['work_hours_estimate']:g}시간의 근로 소득이에요."
    if code == "GOAL_DELAY":
        return f"입력한 계획 저축 기준, {context['goal']['name']} 목표가 약 {context['goal_delay_days']}일 늦어질 수 있어요."
    if code == "CASH_CHOICE":
        return f"등록 가격 {context['price']:,}원"
    if code == "EXPECTED_USE_DAYS":
        return "구매한 뒤 30일 기준으로, 같은 날 여러 번 사용해도 1일로 생각해 주세요."
    return None


def generate_question(session: Session, guest_id: str, simulation_id: str, today: date, ai: PreQuestionAI):
    simulation = session.scalar(
        select(Simulation).where(Simulation.id == simulation_id, Simulation.guest_id == guest_id)
    )
    if simulation is None:
        raise AppError(404, "SIMULATION_NOT_FOUND", "시뮬레이션을 찾을 수 없습니다.")
    existing = session.scalar(
        select(SimulationQuestion).where(
            SimulationQuestion.simulation_id == simulation.id, SimulationQuestion.question_date == today
        )
    )
    if existing:
        return existing
    ensure_active(session, simulation, today)
    context = context_for(session, simulation, today)
    candidates = candidates_for(session, simulation, context)
    session.commit()
    code, wording, method, reason = ai.adapt(candidates, context)
    lock_guest(session, guest_id)
    # A purchase or decision may have arrived while the model was running.
    session.refresh(simulation)
    session.expire(session.get(Item, simulation.item_id))
    existing = session.scalar(
        select(SimulationQuestion).where(
            SimulationQuestion.simulation_id == simulation.id, SimulationQuestion.question_date == today
        )
    )
    if existing:
        session.commit()
        return existing
    ensure_active(session, simulation, today)
    template = BY_CODE[code]
    context.update(template_original=template.original, reference_text=reference_text(code, context))
    question = SimulationQuestion(
        guest_id=guest_id,
        simulation_id=simulation.id,
        question_date=today,
        template_code=code,
        template_version=VERSION,
        stage=template.stage,
        question_text=wording,
        answer_spec=copy.deepcopy(template.answer_spec),
        context_snapshot=context,
        generation_method=method,
        model_id=ai.settings.lablup_model_id if method == "AI" else None,
        fallback_reason=reason,
    )
    session.add(question)
    session.commit()
    return question


def question_output(session: Session, question: SimulationQuestion):
    answer = session.scalar(
        select(SimulationQuestionAnswer).where(SimulationQuestionAnswer.question_id == question.id)
    )
    return {
        "id": question.id,
        "simulation_id": question.simulation_id,
        "question_date": question.question_date,
        "template_code": question.template_code,
        "template_version": question.template_version,
        "stage": question.stage,
        "question": question.question_text,
        "reference_text": question.context_snapshot.get("reference_text"),
        "answer_spec": question.answer_spec,
        "context": question.context_snapshot,
        "generation_method": question.generation_method,
        "fallback_reason": question.fallback_reason,
        "answer": answer_value(answer) if answer else None,
    }


def save_answer(session: Session, guest_id: str, question_id: str, today: date, values: dict):
    lock_guest(session, guest_id)
    question = session.scalar(
        select(SimulationQuestion).where(
            SimulationQuestion.id == question_id, SimulationQuestion.guest_id == guest_id
        )
    )
    if question is None:
        raise AppError(404, "QUESTION_NOT_FOUND", "질문을 찾을 수 없습니다.")
    if question.question_date != today:
        raise AppError(409, "ANSWER_DATE_INVALID", "오늘 질문만 작성하거나 수정할 수 있습니다.")
    ensure_active(session, session.get(Simulation, question.simulation_id), today)
    spec = question.answer_spec
    choice, number, text, obj = (
        values.get(key) for key in ("choice_code", "number_value", "text_value", "object_value")
    )
    valid = False
    if choice is not None:
        valid = choice in {row["code"] for row in spec.get("options", [])}
    elif number is not None:
        valid = spec["type"] == "INTEGER" and spec["minimum"] <= number <= spec["maximum"]
    elif text is not None:
        valid = spec["type"] == "TEXT" and 0 < len(text.strip()) <= spec["max_length"]
    elif obj is not None and spec["type"] == "OBJECT":
        valid = set(obj) == set(spec["fields"]) and all(
            obj[key] in {row["code"] for row in field["options"]} for key, field in spec["fields"].items()
        )
    if not valid:
        raise AppError(422, "ANSWER_NOT_ALLOWED", "이 질문의 응답 형식이나 선택지를 확인해 주세요.")
    answer = session.scalar(
        select(SimulationQuestionAnswer).where(SimulationQuestionAnswer.question_id == question.id)
    )
    if answer is None:
        answer = SimulationQuestionAnswer(question_id=question.id, answered_on=today)
        session.add(answer)
    for field, value in values.items():
        setattr(answer, field, value)
    session.commit()
    return question


def adaptive_result(session: Session, simulation: Simulation, today: date) -> dict:
    from app.services.simulations import observation_dates, simulation_status
    from app.services.reports import item_reasons, reason_statistics

    stop = min(today, simulation.ended_early_on) if simulation.ended_early_on else today
    dates = [day for day in observation_dates(session, simulation) if day <= stop]
    rows = session.execute(
        select(SimulationQuestion, SimulationQuestionAnswer)
        .outerjoin(SimulationQuestionAnswer, SimulationQuestionAnswer.question_id == SimulationQuestion.id)
        .where(SimulationQuestion.simulation_id == simulation.id, SimulationQuestion.question_date.in_(dates))
    ).all()
    by_date = {q.question_date: (q, a) for q, a in rows}
    answers = [a for _, a in rows if a is not None]
    unsure = sum(a.choice_code == "UNSURE" or "UNSURE" in (a.object_value or {}).values() for a in answers)
    definite = len(answers) - unsure
    snapshot = baseline(session, simulation, stop)
    forecast = snapshot["forecast"]
    expected = forecast["expected_used_days"] if forecast else None
    status = simulation_status(simulation, today)
    reason = (
        "EARLY_PURCHASE"
        if simulation.ended_early_on
        else "SIMULATION_NOT_COMPLETE"
        if status != "COMPLETED"
        else "FORECAST_REQUIRED"
        if forecast is None
        else "INSUFFICIENT_ANSWERS"
        if definite * 10 < simulation.duration_days * 7
        else None
    )
    item = session.get(Item, simulation.item_id)
    reasons = item_reasons(item)
    timeline = [
        {"date": day, "question": question_output(session, by_date[day][0]) if day in by_date else None}
        for day in dates
    ]
    summary = f"관찰 {len(dates)}일 중 {len(answers)}일 질문에 답했어요. "
    summary += (
        f"구매 후 30일 중 {expected}일 사용할 것으로 예상했어요."
        if expected is not None
        else "아직 예상 사용일수 답변이 없어요."
    )
    return {
        "simulation_id": simulation.id,
        "status": status,
        "question_mode": "ADAPTIVE",
        "start_date": simulation.start_date,
        "end_date_exclusive": simulation.end_date,
        "duration_days": simulation.duration_days,
        "observed_days": len(dates),
        "answered_days": len(answers),
        "expected_used_days": expected,
        "unused_days": None,
        "unsure_days": unsure,
        "unanswered_days": len(dates) - len(answers),
        "pending_days": 0 if simulation.ended_early_on else simulation.duration_days - len(dates),
        "definite_answer_days": definite,
        "answer_coverage": definite / simulation.duration_days if simulation.duration_days else 0,
        "expected_usage_rate": expected / 30 if expected is not None else None,
        "expected_cost_per_used_day": (simulation.price_snapshot + expected - 1) // expected
        if expected
        else None,
        "price_snapshot": simulation.price_snapshot,
        "comparison_eligible": reason is None,
        "comparison_reason": reason,
        "answer_timeline": timeline,
        "response_summary": summary,
        "purchase_reasons": reasons,
        "reason_history": [
            row for row in reason_statistics(session, simulation.guest_id) if row["reason"] in reasons
        ],
        "metric_unit": "usage_days",
        "prediction_window_days": 30,
        "pre_purchase_baseline": snapshot,
    }


def daily_tasks(session: Session, guest_id: str, today: date, include_answered: bool = False):
    from app.services.simulations import observation_dates

    tasks, completed = [], []
    rows = session.execute(
        select(Item, Simulation)
        .join(Simulation, Simulation.item_id == Item.id)
        .where(Item.guest_id == guest_id, Simulation.question_version == VERSION)
        .order_by(Item.created_at, Item.id)
    )
    for item, simulation in rows:
        if (
            item.decision in ("BUY", "GIVE_UP")
            or simulation.ended_early_on
            or today not in observation_dates(session, simulation)
        ):
            continue
        question = session.scalar(
            select(SimulationQuestion).where(
                SimulationQuestion.simulation_id == simulation.id, SimulationQuestion.question_date == today
            )
        )
        rendered = question_output(session, question) if question else None
        task = {
            "item_id": item.id,
            "simulation_id": simulation.id,
            "name": item.name,
            "category": item.category,
            "current_day": sum(day <= today for day in observation_dates(session, simulation)),
            "duration_days": simulation.duration_days,
            "question": rendered,
            "generate_url": f"/api/v1/simulations/{simulation.id}/daily-question",
        }
        if rendered and rendered["answer"]:
            completed.append(task)
        if not rendered or not rendered["answer"] or include_answered:
            tasks.append(task)
    return tasks, completed
