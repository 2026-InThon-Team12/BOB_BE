"""Synthetic seller-grouped carts: no private screenshots are committed."""

import pytest
from PIL import Image, ImageDraw

from app.services.cart_layout import checkbox_cards
from app.services.cart_geometry import selectors
from app.services.ocr_layout import Line


def grouped_cart(selected=(True, True)):
    image = Image.new("RGB", (1170, 2532), "white")
    draw = ImageDraw.Draw(image)
    for x, y, checked in [(55, 340, True), (86, 460, selected[0]), (86, 1565, True), (86, 1718, selected[1])]:
        draw.rounded_rectangle(
            (x, y, x + 62, y + 62),
            radius=8,
            fill="#00aa55" if checked else "white",
            outline="#999999",
            width=3,
        )
    # Small recurring payment-logo shapes must not become a second cart column.
    for y in (565, 1825):
        draw.rectangle((512, y, 530, y + 18), fill="#00aa55")
    draw.line((84, 1670, 1086, 1670), fill="#555555", width=3)
    words = [
        Line(170, 345, 390, 390, "전체 선택"),
        Line(180, 465, 930, 510, "내일배송 도착 예정"),
        Line(665, 540, 1070, 580, "제주 말차 가루"),
        Line(550, 730, 830, 775, "9% 30,800원"),
        Line(808, 970, 1000, 1010, "30,800원"),
        Line(88, 1290, 295, 1335, "총 배송비"),
        Line(948, 1300, 1080, 1340, "3,000원"),
        Line(115, 1360, 365, 1405, "예상 주문금액"),
        Line(842, 1365, 1080, 1410, "33,800원"),
        Line(169, 1580, 320, 1620, "샘플상점"),
        Line(862, 1575, 1080, 1620, "OF 32271"),  # OCR-garbled coupon action
        Line(177, 1720, 577, 1770, "도착확률 95%"),
        Line(665, 1810, 1080, 1855, "가정용 전기히터"),
        Line(501, 1990, 880, 2035, "33%158,000원"),
        Line(817, 2148, 1080, 2190, "228,700원"),  # Sticky total with unreadable label
    ]
    return image, words


@pytest.mark.parametrize("selected", [(True, True), (True, False), (False, True)])
def test_group_controls_and_payment_logos_never_become_products(selected):
    image, words = grouped_cart(selected)
    cards = checkbox_cards(image, words, words)
    assert len(cards) == 2
    assert [c.selected for c in cards] == list(selected)
    assert [c.price for c in cards] == [30800, 158000]
    assert [c.literal_title() for c in cards] == ["제주 말차 가루", "가정용 전기히터"]


def test_two_different_item_discount_prices_remain_ambiguous():
    image, words = grouped_cart()
    words.append(Line(501, 2050, 880, 2090, "20%140,000원"))
    cards = checkbox_cards(image, words, words)
    assert cards[1].price is None


def test_short_item_without_price_is_not_removed_as_a_seller():
    image, words = grouped_cart()
    words = [w for w in words if w.top < 1565 or w.top >= 1718]
    words.append(Line(169, 1580, 320, 1620, "키보드"))
    cards = checkbox_cards(image, words, words)
    assert len(cards) == 3
    assert cards[1].literal_title() == "키보드" and cards[1].price is None


def test_small_controls_on_aligned_grid_rows_are_not_discarded_as_badges():
    image = Image.new("RGB", (1080, 1100), "white")
    draw = ImageDraw.Draw(image)
    for top in (80, 530):
        draw.rectangle((24, top, 84, top + 60), fill="#00aa55")
        draw.rectangle((564, top, 584, top + 20), fill="#00aa55")
    found = selectors(image)
    assert len(found) == 4
    assert {control.column_left for control in found} == {0, 540}
