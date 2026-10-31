from app.services.cart_discounts import CartDiscountRequest, calculate_cart


def calculation(*, enabled=True, shipping=3000, threshold=40000):
    return calculate_cart(
        CartDiscountRequest.model_validate(
            {
                "items": [
                    {"client_id": "shirt", "name": "티셔츠", "price": 20000, "enabled": True},
                    {"client_id": "socks", "name": "양말", "price": 10000, "enabled": False},
                ],
                "rule": {
                    "minimum_amount": 30000,
                    "unit": "PCT",
                    "value": 10,
                    "free_shipping_enabled": enabled,
                    "shipping_fee": shipping,
                    "free_shipping_minimum": threshold,
                },
            }
        )
    )


def test_discount_screen_returns_both_checkout_breakdowns_and_no_discount_baseline():
    result = calculation()
    assert result["needed"]["breakdown"] == "상품 20,000원 + 배송비 3,000원"
    assert result["all"]["breakdown"] == "상품 30,000원 − 할인 3,000원 + 배송비 3,000원"
    assert result["without_discount"]["breakdown"] == "상품 30,000원 + 배송비 3,000원"
    assert result["without_discount"]["total"] == result["without_discount_total"] == 33000
    assert result["without_discount"]["discount"] == 0
    # Also include the changed coupon amount at the hypothetical shipping threshold.
    assert result["pay_shipping_saves"] == 6000
    assert "6,000원 아껴요" in result["shipping_notice"]


def test_shipping_notice_hides_when_off_and_never_reports_negative_saving():
    assert calculation(enabled=False)["shipping_notice"] is None
    free = calculation(threshold=30000)
    assert free["all"]["shipping"] == 0
    assert free["shipping_notice"] == "무료배송 조건을 채웠어요."
    assert free["all"]["breakdown"].endswith("무료배송")
    cheaper = calculation(threshold=31000)
    assert cheaper["pay_shipping_saves"] == 0
    assert "아껴요" not in cheaper["shipping_notice"]
