import enum
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utc_now
from app.db import Base


def new_id() -> str:
    return str(uuid4())


class Category(str, enum.Enum):
    DAILY_LIVING = "DAILY_LIVING"
    CLOTHING = "CLOTHING"
    FURNITURE = "FURNITURE"
    APPLIANCES = "APPLIANCES"
    DIGITAL = "DIGITAL"
    HOBBY = "HOBBY"
    GAME = "GAME"
    BEAUTY = "BEAUTY"
    FOOD = "FOOD"
    OTHER = "OTHER"


class PurchaseReason(str, enum.Enum):
    ADVERTISEMENT = "ADVERTISEMENT"
    DISCOUNT_LIMITED = "DISCOUNT_LIMITED"
    EXTRA_DISCOUNT = "EXTRA_DISCOUNT"
    STRESS = "STRESS"
    SOCIAL = "SOCIAL"
    NECESSITY = "NECESSITY"
    LONG_WANTED = "LONG_WANTED"
    OTHER = "OTHER"


class ItemSource(str, enum.Enum):
    MANUAL = "MANUAL"
    SCREENSHOT = "SCREENSHOT"
    URL = "URL"
    SHARE = "SHARE"


class Guest(Base):
    __tablename__ = "guests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), unique=True)
    fcm_token: Mapped[str] = mapped_column(String(1024), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RegistrationBatch(Base):
    __tablename__ = "registration_batches"
    __table_args__ = (UniqueConstraint("guest_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (CheckConstraint("price > 0"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(32))
    purchase_reason: Mapped[str] = mapped_column(String(32))
    purchase_reasons: Mapped[list] = mapped_column(JSON, default=list)
    reason_other_text: Mapped[str | None] = mapped_column(String(300))
    sale_ends_on: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    decided_on: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16), default="MANUAL")
    decision: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Simulation(Base):
    __tablename__ = "simulations"
    __table_args__ = (CheckConstraint("duration_days >= 0", name="ck_simulation_duration"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), unique=True)
    price_snapshot: Mapped[int] = mapped_column(BigInteger)
    duration_days: Mapped[int] = mapped_column(Integer)
    base_duration_days: Mapped[int | None] = mapped_column(Integer)
    resume_on: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)  # exclusive
    ended_early_on: Mapped[date | None] = mapped_column(Date)
    measurement_question: Mapped[str] = mapped_column(Text)
    supplemental_question: Mapped[str] = mapped_column(Text)
    question_version: Mapped[str] = mapped_column(String(16), default="v1")
    local_result: Mapped[dict | None] = mapped_column(JSON)


class DailyAnswer(Base):
    __tablename__ = "daily_answers"
    __table_args__ = (
        UniqueConstraint("simulation_id", "answer_date"),
        CheckConstraint("usage_answer IN ('YES', 'NO', 'UNSURE')"),
        CheckConstraint("supplemental_answer IN ('YES', 'NO', 'UNSURE')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    answer_date: Mapped[date] = mapped_column(Date)
    usage_answer: Mapped[str] = mapped_column(String(8))
    supplemental_answer: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (CheckConstraint("actual_price > 0"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), unique=True)
    purchased_on: Mapped[date] = mapped_column(Date)
    actual_price: Mapped[int] = mapped_column(BigInteger)
    review_due_on: Mapped[date] = mapped_column(Date)
    review_start: Mapped[date] = mapped_column(Date)
    review_end: Mapped[date] = mapped_column(Date)  # inclusive
    pre_purchase_snapshot: Mapped[dict | None] = mapped_column(JSON)


class SimulationQuestion(Base):
    __tablename__ = "simulation_questions"
    __table_args__ = (UniqueConstraint("simulation_id", "question_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    question_date: Mapped[date] = mapped_column(Date)
    template_code: Mapped[str] = mapped_column(String(32))
    template_version: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(16))
    question_text: Mapped[str] = mapped_column(Text)
    answer_spec: Mapped[dict] = mapped_column(JSON)
    context_snapshot: Mapped[dict] = mapped_column(JSON)
    generation_method: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str | None] = mapped_column(String(200))
    fallback_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SimulationQuestionAnswer(Base):
    __tablename__ = "simulation_question_answers"
    __table_args__ = (
        CheckConstraint(
            "CASE WHEN choice_code IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN number_value IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN text_value IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN object_value IS NOT NULL THEN 1 ELSE 0 END = 1",
            name="ck_simulation_answer_value",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("simulation_questions.id"), unique=True)
    choice_code: Mapped[str | None] = mapped_column(String(32))
    number_value: Mapped[int | None] = mapped_column(Integer)
    text_value: Mapped[str | None] = mapped_column(String(300))
    object_value: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    answered_on: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class PurchaseReview(Base):
    __tablename__ = "purchase_reviews"
    __table_args__ = (CheckConstraint("used_days BETWEEN 0 AND 14", name="ck_review_used_days"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), unique=True)
    used_days: Mapped[int | None] = mapped_column(Integer)
    frequency: Mapped[str | None] = mapped_column(String(16))
    submitted_on: Mapped[date] = mapped_column(Date)


class PurchaseQuestion(Base):
    __tablename__ = "purchase_questions"
    __table_args__ = (UniqueConstraint("purchase_id", "question_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    purchase_id: Mapped[str] = mapped_column(ForeignKey("purchases.id"), index=True)
    question_date: Mapped[date] = mapped_column(Date)
    template_code: Mapped[str] = mapped_column(String(32))
    template_version: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(16))
    question_text: Mapped[str] = mapped_column(Text)
    answer_spec: Mapped[dict] = mapped_column(JSON)
    context_snapshot: Mapped[dict] = mapped_column(JSON)
    generation_method: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str | None] = mapped_column(String(200))
    fallback_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PurchaseQuestionAnswer(Base):
    __tablename__ = "purchase_question_answers"
    __table_args__ = (
        CheckConstraint("(choice_code IS NULL) <> (number_value IS NULL)", name="ck_purchase_answer_value"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(ForeignKey("purchase_questions.id"), unique=True)
    choice_code: Mapped[str | None] = mapped_column(String(32))
    number_value: Mapped[int | None] = mapped_column(BigInteger)
    answered_on: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("guest_id", "month"),
        CheckConstraint("income >= 0 AND food >= 0 AND living >= 0 AND transport >= 0"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    month: Mapped[str] = mapped_column(String(7))
    income: Mapped[int] = mapped_column(BigInteger)
    food: Mapped[int] = mapped_column(BigInteger)
    living: Mapped[int] = mapped_column(BigInteger)
    transport: Mapped[int] = mapped_column(BigInteger)
    savings: Mapped[int] = mapped_column(BigInteger, default=0)
    hourly_wage: Mapped[int | None] = mapped_column(BigInteger)


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("target_amount > 0"),
        Index(
            "uq_goal_active_guest",
            "guest_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active = 1"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    target_amount: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    completed_on: Mapped[date | None] = mapped_column(Date)
    achieved_on: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    purchased_on: Mapped[date | None] = mapped_column(Date)
    actual_price: Mapped[int | None] = mapped_column(BigInteger)
    purchase_id: Mapped[str | None] = mapped_column(ForeignKey("purchases.id"), unique=True)
    shelf_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Impulse(Base):
    __tablename__ = "impulses"
    __table_args__ = (UniqueConstraint("guest_id", "idempotency_key"), CheckConstraint("amount > 0"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"))
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[int] = mapped_column(BigInteger)
    decision: Mapped[str] = mapped_column(String(16), default="PENDING")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    budget_month: Mapped[str] = mapped_column(String(7))
    available_amount: Mapped[int | None] = mapped_column(BigInteger)
    delay_days: Mapped[int | None] = mapped_column(BigInteger)
    comparison_reason: Mapped[str | None] = mapped_column(String(40))
    if_resisted_on: Mapped[date | None] = mapped_column(Date)
    if_purchased_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SavingEvent(Base):
    __tablename__ = "saving_events"
    __table_args__ = (
        UniqueConstraint("impulse_id", "kind"),
        UniqueConstraint("item_id", "kind"),
        CheckConstraint("(impulse_id IS NULL) <> (item_id IS NULL)", name="ck_saving_source"),
        CheckConstraint("(kind = 'CREDIT' AND amount > 0) OR (kind = 'REVERSAL' AND amount < 0)"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"), index=True)
    impulse_id: Mapped[str | None] = mapped_column(ForeignKey("impulses.id"))
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id"))
    occurred_on: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GoalReplacement(Base):
    __tablename__ = "goal_replacements"
    __table_args__ = (UniqueConstraint("guest_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    from_goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), unique=True)
    to_goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    carry_savings: Mapped[bool] = mapped_column(Boolean)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GoalSavingTransfer(Base):
    """Move allocations of an original credit without creating new savings."""

    __tablename__ = "goal_saving_transfers"
    __table_args__ = (
        UniqueConstraint("replacement_id", "credit_id"),
        CheckConstraint("amount > 0", name="ck_goal_transfer_amount"),
        CheckConstraint("from_goal_id <> to_goal_id", name="ck_goal_transfer_destination"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    replacement_id: Mapped[str] = mapped_column(ForeignKey("goal_replacements.id"))
    credit_id: Mapped[str] = mapped_column(ForeignKey("saving_events.id"), index=True)
    from_goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    to_goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SimulationExtension(Base):
    __tablename__ = "simulation_extensions"
    __table_args__ = (UniqueConstraint("simulation_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("guest_id", "idempotency_key"),
        CheckConstraint("amount > 0"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))
    purchase_id: Mapped[str | None] = mapped_column(ForeignKey("purchases.id"), unique=True)
    impulse_id: Mapped[str | None] = mapped_column(ForeignKey("impulses.id"), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[int] = mapped_column(BigInteger)
    spent_on: Mapped[date] = mapped_column(Date, index=True)
    memo: Mapped[str | None] = mapped_column(String(500))
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DiscountComparison(Base):
    __tablename__ = "discount_comparisons"
    __table_args__ = (UniqueConstraint("guest_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    decision: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NotificationDispatch(Base):
    __tablename__ = "notification_dispatches"
    __table_args__ = (UniqueConstraint("guest_id", "local_date", "kind"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"), index=True)
    local_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(32), default="DAILY_DIGEST")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
