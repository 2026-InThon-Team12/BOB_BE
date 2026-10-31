from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator
from app.schemas import RequestModel


class SimulationAnswerInput(RequestModel):
    choice_code: str | None = Field(default=None, min_length=1, max_length=32)
    number_value: Annotated[int, Field(strict=True, ge=0, le=30)] | None = None
    text_value: str | None = Field(default=None, min_length=1, max_length=300)
    object_value: dict[str, str] | None = None

    @model_validator(mode="after")
    def one_value(self):
        if (
            sum(
                getattr(self, field) is not None
                for field in ("choice_code", "number_value", "text_value", "object_value")
            )
            != 1
        ):
            raise ValueError("Provide exactly one answer value")
        if self.text_value is not None:
            self.text_value = self.text_value.strip()
            if not self.text_value:
                raise ValueError("Text cannot be empty")
        return self


class SimulationQuestionOutput(BaseModel):
    id: str
    simulation_id: str
    question_date: date
    template_code: str
    template_version: str
    stage: Literal["TRIGGER", "NECESSITY", "PREDICTION", "VALUE", "PERSISTENCE"]
    question: str
    reference_text: str | None
    answer_spec: dict
    context: dict
    generation_method: Literal["AI", "TEMPLATE"]
    fallback_reason: str | None
    answer: dict | None


class SimulationQuestionHistory(BaseModel):
    items: list[SimulationQuestionOutput]
    baseline: dict
    limit: int
    offset: int


class PurchaseAnswerInput(RequestModel):
    choice_code: str | None = Field(default=None, min_length=1, max_length=32)
    number_value: Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)] | None = None

    @model_validator(mode="after")
    def one_value(self):
        if (self.choice_code is None) == (self.number_value is None):
            raise ValueError("Provide exactly one answer value")
        return self


class PurchaseAnswerOutput(BaseModel):
    choice_code: str | None
    number_value: int | None
    answered_on: date


class PurchaseQuestionOutput(BaseModel):
    id: str
    purchase_id: str
    question_date: date
    template_code: str
    template_version: str
    stage: Literal["IMMEDIATE", "USAGE", "VALUE", "MOTIVE", "LONG_TERM"]
    question: str
    reference_text: str | None
    answer_spec: dict
    context: dict
    generation_method: Literal["AI", "TEMPLATE"]
    fallback_reason: str | None
    answer: PurchaseAnswerOutput | None
    comparison: dict | None


class PurchaseQuestionHistory(BaseModel):
    items: list[PurchaseQuestionOutput]
    usage_summary: dict
    limit: int
    offset: int
