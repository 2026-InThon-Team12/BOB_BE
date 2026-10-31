"""Synthetic large-selector cart with inline discount, unit and checkout prices."""

import io
import json

from PIL import Image, ImageDraw

from app.core.config import Settings
from app.services.ai import AiService
from app.services.cart_layout import card_price_values, cart_selectors, checkbox_cards
from app.services.ocr_layout import Line


def fixture(select_all=False):
    image = Image.new("RGB", (1080, 2504), "white")
    draw = ImageDraw.Draw(image)
    for top, selected in ((156, select_all), (434, False), (1430, True)):
        draw.rounded_rectangle(
            (46, top, 123, top + 77),
            radius=6,
            fill="#346aff" if selected else "white",
            outline="#aaaaaa",
            width=3,
        )
        # Unselected Coupang controls contain a pale grey check, not an empty box.
        draw.line(
            (61, top + 41, 77, top + 56, 108, top + 23), fill="white" if selected else "#eeeeee", width=7
        )
    for y in (261, 388, 1038, 1383, 2165, 2211, 2333):
        draw.line((0, y, 1079, y), fill="#dddddd", width=2)
    draw.line((466, 675, 596, 675), fill="#999999", width=2)
    lines = [
        Line(41, 153, 300, 240, "전체 선택"),
        Line(900, 160, 1032, 233, "선택 삭제"),
        Line(150, 426, 1023, 501, "샘플 갤럭시탭 탈부착 종이질감필름 저반사"),
        Line(149, 500, 423, 553, "옵션: 갤럭시탭 S8"),
        Line(469, 602, 771, 642, "11/4 도착 예정"),
        Line(466, 663, 596, 688, "18,9002!"),
        Line(469, 683, 754, 760, "15% 15,9002"),
        Line(468, 757, 699, 810, "(1개당 15,900원)"),
        Line(339, 1099, 737, 1151, "같은 판매자 상품 추가하기"),
        Line(46, 1218, 314, 1312, "로켓배송 상품"),
        Line(148, 1425, 445, 1487, "화이트 치아미백제"),
        Line(149, 1500, 370, 1538, "옵션: 80g, 4개"),
        Line(467, 1592, 678, 1623, "한달구매 700+"),
        Line(469, 1662, 672, 1702, "내일 도착"),
        Line(466, 1729, 819, 1762, "01:12:19 내 주문 시"),
        Line(467, 1798, 733, 1850, "쿠폰할인 22,970원"),
        Line(466, 1820, 996, 1907, "DY 21,9808! 로켓 내일"),
        Line(468, 1914, 664, 1947, "(10g 6872!)"),
        Line(468, 1965, 816, 1997, "990원 쿠폰할인 적용됨"),
        Line(799, 2233, 1042, 2306, "26,990원"),
        Line(102, 2255, 308, 2286, "990원 할인됨"),
        Line(337, 2394, 741, 2461, "총 2개 상품 구매하기"),
    ]
    return image, lines


def test_large_blue_checkbox_and_grey_placeholder_tick_are_distinguished():
    image, lines = fixture()
    assert [selected for _, selected in cart_selectors(image)] == [False, False, True]
    cards = checkbox_cards(image, lines)
    assert [(c.selected, c.price) for c in cards] == [(False, 15900), (True, 21980)]
    assert cards[1].literal_title() == "화이트 치아미백제"
    assert "갤럭시탭" in cards[0].literal_title()


def test_checked_select_all_header_does_not_become_a_product():
    image, lines = fixture(select_all=True)
    cards = checkbox_cards(image, lines)
    assert len(cards) == 2
    assert [c.price for c in cards if c.selected] == [21980]


def test_inline_sale_price_does_not_include_unit_or_coupon_savings():
    assert card_price_values("4% 21,980원 로켓 내일") == {21980}
    assert card_price_values("DY 21,9808! ~ 22 ua") == {21980}
    assert card_price_values("15% 15,9002") == {15900}
    for text in (
        "(1개당 15,900원)",
        "(10g 6872!)",
        "쿠폰할인 22,970원",
        "990원 쿠폰할인 적용됨",
        "990원 할인됨",
        "총 결제액 26,990원",
    ):
        assert card_price_values(text) == set()


def test_api_proposal_contains_only_selected_product_and_ignores_checkout_total():
    image, lines = fixture()
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right - line.left}\t{line.bottom - line.top}\t95\t{line.text}"
        for line in lines
    )

    class Client:
        def message(self, **kwargs):
            assert "갤럭시탭" not in kwargs["content"][0]["text"]
            return json.dumps(
                {
                    "items": [
                        {
                            "card_id": 0,
                            "name": "화이트 치아미백제",
                            "category": "BEAUTY",
                            "supplemental_question": "오늘 사용할 상황이 있었나요?",
                        }
                    ]
                }
            )

    data = io.BytesIO()
    image.save(data, format="PNG")
    result = AiService(
        Settings(_env_file=None), client=Client(), ocr=lambda image, settings: table
    ).extract_cart(data.getvalue())
    assert [(i.name, i.price) for i in result.items] == [("화이트 치아미백제", 21980)]
