from datetime import date

from app.core.errors import AppError
from app.models import PurchaseReason
from app.schemas import BatchRequest
from app.services.cart_discounts import CartDiscountRequest, CartLine, calculate_cart


def scheduled_days(price: int, sale_ends_on: date | None, today: date) -> int:
    base = 3 if price <= 30_000 else 7 if price < 100_000 else 14
    if sale_ends_on is None:
        return base
    if sale_ends_on < today:
        raise AppError(422, "SALE_ALREADY_ENDED", "오늘 또는 이후의 마감일을 선택해 주세요.")
    return min(base, (sale_ends_on - today).days)


def preview_registration(body: BatchRequest, today: date) -> dict:
    needed = any(PurchaseReason.EXTRA_DISCOUNT in item.purchase_reasons for item in body.items)
    review = body.discount_review
    discount = None
    indexes = list(range(len(body.items)))
    if review:
        if any(index >= len(body.items) for index in review.needed_item_indexes):
            raise AppError(422, "INVALID_ITEM_INDEX", "장바구니 품목 번호를 확인해 주세요.")
        discount = calculate_cart(
            CartDiscountRequest(
                rule=review.rule,
                items=[
                    CartLine(
                        client_id=str(i),
                        name=item.name,
                        price=item.price,
                        purchase_reasons=item.purchase_reasons,
                        enabled=i in review.needed_item_indexes,
                    )
                    for i, item in enumerate(body.items)
                ],
            )
        )
        discount["decision"] = review.decision
        if review.decision == "NEEDED":
            indexes = review.needed_item_indexes
    return {
        "discount_check_required": needed and review is None,
        "discount": discount,
        "selected_item_indexes": indexes,
        "items": [
            {
                "index": i,
                "name": item.name,
                "sale_ends_on": item.sale_ends_on,
                "duration_days": scheduled_days(item.price, item.sale_ends_on, today),
                "decision_ready": item.sale_ends_on == today,
            }
            for i, item in enumerate(body.items)
        ],
    }
