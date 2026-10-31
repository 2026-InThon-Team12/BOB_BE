from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import Category, ItemSource, PurchaseReason
from app.services.cart_discounts import BatchDiscountReview

Money = Annotated[int, Field(strict=True, gt=0, le=1_000_000_000_000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Answer = Literal["YES", "NO", "UNSURE"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PushTokenRequest(RequestModel):
    token: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=10, max_length=1024, pattern=r"^[A-Za-z0-9_:\-]+$"
        ),
    ]
    enabled: bool = True


class ItemInput(RequestModel):
    name: Name
    price: Money
    category: Category
    purchase_reason: PurchaseReason | None = Field(default=None, json_schema_extra={"deprecated": True})
    purchase_reasons: list[PurchaseReason] = Field(default_factory=list, max_length=8)
    reason_other_text: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None
    ) = None
    sale_ends_on: date | None = None
    source: ItemSource = ItemSource.MANUAL
    source_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    image_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    supplemental_question: Question | None = None
    question_mode: Literal["LOCAL", "ADAPTIVE", "LEGACY"] = "LOCAL"

    @model_validator(mode="after")
    def reasons(self):
        if self.source == ItemSource.URL and not self.source_url:
            raise ValueError("URL registration requires source_url")
        if not self.purchase_reasons and self.purchase_reason:
            self.purchase_reasons = [self.purchase_reason]
        if not self.purchase_reasons or len(set(self.purchase_reasons)) != len(self.purchase_reasons):
            raise ValueError("Choose one or more distinct reasons")
        if self.purchase_reason and self.purchase_reason not in self.purchase_reasons:
            raise ValueError("Conflicting purchase reasons")
        self.purchase_reasons = sorted(self.purchase_reasons, key=lambda value: value.value)
        self.purchase_reason = self.purchase_reasons[0]
        if (PurchaseReason.OTHER in self.purchase_reasons) != (self.reason_other_text is not None):
            raise ValueError("OTHER requires reason_other_text; omit it for other reasons")
        if (PurchaseReason.DISCOUNT_LIMITED in self.purchase_reasons) != (self.sale_ends_on is not None):
            raise ValueError("DISCOUNT_LIMITED requires sale_ends_on")
        return self


class BatchRequest(RequestModel):
    items: list[ItemInput] = Field(min_length=1, max_length=20)
    discount_review: BatchDiscountReview | None = None


class ItemPatch(RequestModel):
    name: Name | None = None
    price: Money | None = None
    category: Category | None = None
    purchase_reason: PurchaseReason | None = None
    purchase_reasons: list[PurchaseReason] | None = Field(default=None, min_length=1, max_length=8)
    reason_other_text: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None
    ) = None
    sale_ends_on: date | None = None
    supplemental_question: Question | None = None


class AnswerRequest(RequestModel):
    usage_answer: Answer
    supplemental_answer: Answer = Field(default="UNSURE", json_schema_extra={"deprecated": True})


class DecisionRequest(RequestModel):
    decision: Literal["BUY", "HOLD", "GIVE_UP"]
    purchased_on: date | None = None
    actual_price: Money | None = None


class ReviewRequest(RequestModel):
    frequency: Literal["ALMOST_DAILY", "OFTEN", "SOMETIMES", "NEVER"] | None = None
    used_days: Annotated[int, Field(strict=True, ge=0, le=14)] | None = Field(
        default=None, json_schema_extra={"deprecated": True}
    )

    @model_validator(mode="after")
    def one_measurement(self):
        if (self.frequency is None) == (self.used_days is None):
            raise ValueError("Provide frequency or legacy exact used_days, not both")
        return self


class GuestSessionOutput(BaseModel):
    guest_id: str
    access_token: str
    token_type: Literal["bearer"]


class PushTokenOutput(BaseModel):
    enabled: bool


class AnswerOption(BaseModel):
    value: Answer
    label: str


SimulationStatus = Literal["SCHEDULED", "ACTIVE", "COMPLETED", "EARLY_PURCHASED", "EARLY_GIVE_UP", "ON_HOLD"]


class SimulationSummary(BaseModel):
    id: str
    status: SimulationStatus
    start_date: date
    end_date_exclusive: date
    observation_end_date_exclusive: date | None = None
    termination_reason: Literal["EARLY_PURCHASE", "EARLY_GIVE_UP"] | None = None
    duration_days: int
    observation_dates: list[date] = Field(default_factory=list)
    measurement_question: str
    supplemental_question: str
    answer_options: list[AnswerOption]
    current_day: int = 0
    question: str = ""
    question_mode: Literal["LOCAL", "ADAPTIVE", "LEGACY"] = "LEGACY"


class SimulationResult(BaseModel):
    simulation_id: str
    status: SimulationStatus
    start_date: date
    end_date_exclusive: date
    observation_end_date_exclusive: date | None = None
    termination_reason: Literal["EARLY_PURCHASE", "EARLY_GIVE_UP"] | None = None
    duration_days: int
    observed_days: int
    answered_days: int | None
    expected_used_days: int | None
    unused_days: int | None
    unsure_days: int | None
    unanswered_days: int | None
    pending_days: int
    definite_answer_days: int | None
    answer_coverage: float | None
    expected_usage_rate: float | None
    expected_cost_per_used_day: int | None
    price_snapshot: int
    comparison_eligible: bool
    comparison_reason: str | None
    answer_timeline: list[dict] = Field(default_factory=list)
    response_summary: str = ""
    answer_state: Literal["SERVER_RECORDED", "CLIENT_MANAGED", "AGGREGATED"] = "SERVER_RECORDED"
    purchase_reasons: list[PurchaseReason] = Field(default_factory=list)
    reason_history: list[dict] = Field(default_factory=list)
    metric_unit: str = "usage_days"
    question_mode: Literal["LOCAL", "ADAPTIVE", "LEGACY"] = "LEGACY"
    prediction_window_days: int | None = None
    pre_purchase_baseline: dict | None = None
    reflection_counts: dict[str, int] | None = None
    purchase_support_rate: float | None = None


class ReviewOutput(BaseModel):
    used_days: int | None
    frequency: str | None = None
    min_days: float = 0
    max_days: float = 0
    estimated_days: float = 0
    submitted_on: date
    late_days: int


class PurchaseOutput(BaseModel):
    question_mode: Literal["LOCAL", "ADAPTIVE", "LEGACY"] = "LEGACY"
    id: str
    item_id: str
    purchased_on: date
    actual_price: int
    review_due_on: date
    review_start: date
    review_end: date
    review: ReviewOutput | None
    actual_usage_rate: float | None
    expected_usage_rate: float | None
    difference_percentage_points: float | None
    comparison_reason: str | None
    item_name: str = ""
    category: str = ""
    review_options: list[dict] = Field(default_factory=list)
    expected_days_in_review_window: float | None = None
    actual_days_estimate: float | None = None
    overprediction_ratio_estimate: float | None = None
    estimate_method: str = "range_midpoint"
    is_estimate: bool = True
    verdict: str | None = None
    pre_purchase_baseline: dict | None = None


class ItemOutput(BaseModel):
    id: str
    name: str
    price: int
    category: Category
    purchase_reason: PurchaseReason
    purchase_reasons: list[PurchaseReason] = Field(default_factory=list)
    reason_other_text: str | None = None
    sale_ends_on: date | None = None
    image_url: str | None = None
    source_url: str | None = None
    wallet_comparison: dict | None = None
    source: ItemSource
    decision: Literal["BUY", "HOLD", "GIVE_UP"] | None
    decided_on: date | None = None
    simulation: SimulationSummary
    purchase: PurchaseOutput | None


class BatchOutput(BaseModel):
    items: list[ItemOutput]
    discount: dict | None = None


class ItemListOutput(BatchOutput):
    limit: int
    offset: int


class QuestionCard(BaseModel):
    item_id: str
    simulation_id: str
    name: str
    answer_date: date
    measurement_question: str
    supplemental_question: str
    answer_options: list[AnswerOption]
    question: str = ""
    category: str = ""
    current_day: int = 0
    duration_days: int = 0
    calibration: dict | None = None
    answer: str | None = None


class DecisionCard(BaseModel):
    item_id: str
    name: str
    decision: Literal["HOLD"] | None
    result: SimulationResult


class TodayOutput(BaseModel):
    date: date
    questions: list[QuestionCard]
    decisions: list[DecisionCard]
    reviews: list[PurchaseOutput]
    answered: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    next_notification_at: str = ""
    purchase_questions: list[dict] = Field(default_factory=list)
    purchase_answered: list[dict] = Field(default_factory=list)
    pre_purchase_questions: list[dict] = Field(default_factory=list)
    pre_purchase_answered: list[dict] = Field(default_factory=list)


class AnswerOutput(BaseModel):
    answer_date: date
    usage_answer: Answer
    supplemental_answer: Answer
