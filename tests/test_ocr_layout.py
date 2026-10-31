from app.services.ocr_layout import grounded_prices, money_values, visual_lines


def table(*rows):
    return "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{x}\t{y}\t{width}\t20\t95\t{text}" for x, y, width, text in rows
    )


def test_ably_shifted_prices_are_grounded_in_own_card_even_if_next_row_is_unchecked():
    # Regression for the user's report: 27,500 from 미우블랑 shifted to 어바웃영,
    # then 어바웃영's 20,500 shifted to the following unchecked cart row.
    lines = visual_lines(
        table(
            (10, 10, 150, "미우블랑"),
            (10, 50, 150, "27,500원"),
            (10, 130, 150, "어바웃영"),
            (10, 170, 150, "20,500원"),
            (10, 250, 180, "체크 해제 상품"),
            (10, 290, 150, "15,000원"),
        )
    )
    items, changed = grounded_prices(
        [
            {"name": "미우블랑", "price": 27500},
            {"name": "어바웃영", "price": 27500},
            {"name": "체크 해제 상품", "price": 20500},
        ],
        lines,
    )
    assert [item["price"] for item in items] == [27500, 20500, 15000]
    assert changed


def test_unclear_own_price_is_null_not_borrowed_from_next_product():
    lines = visual_lines(
        table(
            (10, 10, 150, "어바웃영"),
            (10, 50, 150, "20,500원 23,500원"),
            (10, 130, 150, "다음 상품"),
            (10, 170, 150, "27,500원"),
        )
    )
    items, changed = grounded_prices(
        [{"name": "어바웃영", "price": 27500}, {"name": "다음 상품", "price": 27500}], lines
    )
    assert items[0]["price"] is None
    assert items[1]["price"] == 27500
    assert changed


def test_ocr_block_order_and_model_order_do_not_change_screen_order():
    lines = visual_lines(
        table(
            (10, 170, 150, "20,500원"),
            (10, 50, 150, "27,500원"),
            (10, 130, 150, "어바웃영"),
            (10, 10, 150, "미우블랑"),
        )
    )
    items, _ = grounded_prices(
        [{"name": "어바웃영", "price": 20500}, {"name": "미우블랑", "price": 27500}], lines
    )
    assert [item["name"] for item in items] == ["미우블랑", "어바웃영"]


def test_no_evidence_unknown_price_duplicate_title_and_shipping_are_not_guessed():
    lines = visual_lines(table((10, 10, 150, "코트"), (10, 50, 150, "배송비 3,000원")))
    assert grounded_prices([{"name": "코트", "price": 3000}], lines)[0][0]["price"] is None
    assert grounded_prices([{"name": "인식 안 된 이름", "price": 3000}], lines)[0][0]["price"] is None
    assert grounded_prices([{"name": "코트", "price": None}], lines)[0][0]["price"] is None
    assert (
        grounded_prices([{"name": "코트", "price": 3000}, {"name": "코트", "price": 3000}], lines)[0][0][
            "price"
        ]
        is None
    )
    assert money_values("총 결제 20,500원") == set()
    assert money_values("할부 월 5,000원") == set()


def test_wrapped_title_and_separate_columns_keep_local_price_ownership():
    lines = visual_lines(
        table(
            (10, 10, 120, "무선"),
            (10, 35, 120, "키보드"),
            (10, 65, 120, "30,000원"),
            (300, 10, 120, "이어폰"),
            (300, 65, 120, "10,000원"),
        )
    )
    items, _ = grounded_prices(
        [{"name": "무선 키보드", "price": 10000}, {"name": "이어폰", "price": 30000}], lines
    )
    assert {item["name"]: item["price"] for item in items} == {"무선 키보드": 30000, "이어폰": 10000}
