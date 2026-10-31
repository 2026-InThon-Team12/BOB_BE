"""The handoff's item-switch checkout. No AI, inferred shipping, or money writes."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import PurchaseReason


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Money = Annotated[int, Field(strict=True, gt=0, le=1_000_000_000_000)]
Name = Annotated[str, Field(min_length=1, max_length=200)]

Amount = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)]


class CartDiscountRule(RequestModel):
    minimum_amount: Amount
    unit: Literal["PCT", "WON"]
    value: Amount
    shipping_fee: Amount = 0
    free_shipping_minimum: Amount | None = None
    free_shipping_enabled: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def valid_rate(self):
        if self.unit == "PCT" and self.value > 100:
            raise ValueError("percentage must be between 0 and 100")
        if self.free_shipping_enabled is None:
            object.__setattr__(self, "free_shipping_enabled", self.free_shipping_minimum is not None)
        if "free_shipping_enabled" in self.model_fields_set:
            if "shipping_fee" not in self.model_fields_set:
                raise ValueError("Provide the base shipping fee, also when the free shipping toggle is on")
            if self.free_shipping_enabled and self.free_shipping_minimum is None:
                raise ValueError("Free shipping ON requires the threshold")
        return self


class CartLine(RequestModel):
    client_id: str = Field(min_length=1, max_length=100)
    name: Name
    price: Money
    purchase_reasons: list[PurchaseReason] = Field(default_factory=list, max_length=8)
    enabled: bool | None = None


class CartDiscountRequest(RequestModel):
    items: list[CartLine] = Field(min_length=1, max_length=20)
    rule: CartDiscountRule

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.client_id for item in self.items}) != len(self.items):
            raise ValueError("client_id must be unique within the cart")
        return self


class BatchDiscountReview(RequestModel):
    rule: CartDiscountRule
    needed_item_indexes: list[Annotated[int, Field(strict=True, ge=0, le=19)]] = Field(max_length=20)
    decision: Literal["NEEDED", "ALL"]

    @model_validator(mode="after")
    def unique_indexes(self):
        if len(set(self.needed_item_indexes)) != len(self.needed_item_indexes):
            raise ValueError("duplicate item indexes")
        return self


def checkout(subtotal: int, rule: CartDiscountRule) -> dict:
    eligible = subtotal > 0 and subtotal >= rule.minimum_amount
    discount = 0
    if eligible:
        # Positive integer half-up rounding, matching JavaScript Math.round.
        discount = (subtotal * rule.value + 50) // 100 if rule.unit == "PCT" else rule.value
        discount = min(subtotal, discount)
    shipping = rule.shipping_fee if subtotal else 0
    if rule.free_shipping_enabled and subtotal >= rule.free_shipping_minimum:
        shipping = 0
    return {
        "subtotal": subtotal,
        "discount_eligible": eligible,
        "discount": discount,
        "shipping": shipping,
        "total": subtotal - discount + shipping,
        "breakdown": checkout_breakdown(subtotal, discount, shipping),
    }


def checkout_breakdown(subtotal: int, discount: int, shipping: int) -> str:
    if not subtotal:
        return "담은 물건이 없어요"
    return (
        f"상품 {subtotal:,}원"
        + (f" − 할인 {discount:,}원" if discount else "")
        + (f" + 배송비 {shipping:,}원" if shipping else " · 무료배송")
    )


def calculate_cart(data: CartDiscountRequest) -> dict:
    lines = [
        {
            **item.model_dump(mode="json"),
            "enabled": item.enabled
            if item.enabled is not None
            else PurchaseReason.EXTRA_DISCOUNT not in item.purchase_reasons,
        }
        for item in data.items
    ]
    needed = checkout(sum(item["price"] for item in lines if item["enabled"]), data.rule)
    whole = checkout(sum(item["price"] for item in lines), data.rule)
    extra = whole["total"] - needed["total"]
    trap = any(not item["enabled"] for item in lines) and extra > 0
    gap = (
        max(0, data.rule.free_shipping_minimum - whole["subtotal"])
        if data.rule.free_shipping_enabled
        else None
    )
    # Only claim a saving when the hypothetical threshold checkout really costs more.
    shipping_saving = None
    if gap and whole["shipping"]:
        shipping_saving = max(0, checkout(whole["subtotal"] + gap, data.rule)["total"] - whole["total"])
    no_discount = {
        **whole,
        "discount": 0,
        "discount_eligible": False,
        "total": whole["subtotal"] + whole["shipping"],
        "breakdown": checkout_breakdown(whole["subtotal"], 0, whole["shipping"]),
    }
    shipping_message = None
    if data.rule.free_shipping_enabled:
        shipping_message = (
            "무료배송 조건을 채웠어요." if not gap else f"무료배송까지 {gap:,}원 더 담아야 해요."
        )
        if shipping_saving:
            shipping_message += f" 배송비 {whole['shipping']:,}원을 내는 쪽이 {shipping_saving:,}원 아껴요."
    return {
        "items": lines,
        "rule": data.rule.model_dump(),
        "needed": needed,
        "all": whole,
        "without_discount": no_discount,
        "needed_item_ids": [item["client_id"] for item in lines if item["enabled"]],
        "discount_check_required": any(
            PurchaseReason.EXTRA_DISCOUNT in item.purchase_reasons for item in data.items
        ),
        "verdict": "TRAP" if trap else "SMART",
        "extra_spending": extra,
        "without_discount_total": whole["subtotal"] + whole["shipping"],
        "saved_amount": whole["discount"],
        "remaining_to_free_shipping": gap,
        "pay_shipping_saves": shipping_saving,
        "shipping_notice": shipping_message,
        "message": f"할인 받으려고 {extra:,}원을 더 쓰게 돼요"
        if trap
        else f"어차피 살 물건이라 {whole['discount']:,}원 아꼈어요",
    }
