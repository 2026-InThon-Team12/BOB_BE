"""Synthetic vertical cart: no user screenshots or shopping records are stored."""

import io
import json

import pytest
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.services.ai import AiService
from app.services.cart_layout import CartCard, checkbox_cards, name_belongs_to_card, selector_rows
from app.services.ocr_layout import Line, money_values


def cart_fixture(selected=(True, True, False)):
    image = Image.new("RGB", (1080, 2504), "white")
    draw = ImageDraw.Draw(image)
    for top, checked in zip((302, 416, 1024, 1632), (False, *selected)):
        draw.rounded_rectangle(
            (44, top, 94, top + 50),
            radius=6,
            fill="#222222" if checked else "white",
            outline="#cccccc",
            width=4,
        )
    lines = [Line(137, 310, 347, 345, "전체 선택 (3/19)")]
    for start, name, price in (
        (416, "코튼 패드 청바지", "27,5009!"),
        (1024, "데일리 후드 집업", "20,500 원"),
        (1632, "레드 스티치 와이드 데님", "29,500 운"),
    ):
        lines += [
            Line(386, start, 493, start + 29, "샘플 상점"),
            Line(386, start + 55, 970, start + 95, name),
            Line(402, start + 120, 886, start + 160, "오늘 출발 주문 마감"),
            Line(880, start + 447, 1033, start + 487, price),
        ]
    lines += [
        Line(855, 2177, 1021, 2243, "쿠폰 선택"),
        Line(301, 2352, 777, 2420, "81,500원 61,770원 주문하기"),
    ]
    return image, lines


def test_checked_unchecked_and_select_all_do_not_shift_card_prices():
    image, lines = cart_fixture()
    cards = checkbox_cards(image, lines)
    assert len(selector_rows(image)) == 4
    assert [card.price for card in cards] == [27500, 20500, 29500]
    assert [card.selected for card in cards] == [True, True, False]
    assert [card.lines[1].text for card in cards] == [
        "코튼 패드 청바지",
        "데일리 후드 집업",
        "레드 스티치 와이드 데님",
    ]
    assert all("61,770" not in line.text for card in cards for line in card.lines)


def test_struck_out_price_is_not_used_and_unknown_prices_remain_unknown():
    image, lines = cart_fixture()
    old_price = Line(880, 805, 1033, 835, "34,400원")
    ImageDraw.Draw(image).line((880, 820, 1033, 820), fill="#cccccc", width=3)
    cards = checkbox_cards(image, sorted(lines + [old_price], key=lambda line: line.top))
    assert cards[0].price == 27500
    # Two non-struck current prices have no safe single interpretation.
    image, lines = cart_fixture()
    cards = checkbox_cards(
        image, sorted(lines + [Line(880, 810, 1033, 840, "25,000원")], key=lambda line: line.top)
    )
    assert cards[0].price is None


def test_layout_without_repeated_checkboxes_uses_generic_path():
    _, lines = cart_fixture()
    assert checkbox_cards(Image.new("RGB", (1080, 2504), "white"), lines) == []


def test_order_totals_and_ocr_won_glyph_are_handled_without_inventing_amounts():
    assert money_values("81,500원 61,770원 주 문 하 기") == set()
    assert money_values("61,770원 결 제 하 기") == set()
    assert money_values("27,5009!") == {27500}
    assert money_values("20,5002!") == {20500}
    assert money_values("20,5008.") == {20500}
    assert money_values("27,5009") == set()


def test_unreadable_price_keeps_the_product_in_its_original_position():
    image, lines = cart_fixture()
    lines = [
        Line(line.left, line.top, line.right, line.bottom, "가격 읽기 불가")
        if line.text == "27,5009!"
        else line
        for line in lines
    ]
    cards = checkbox_cards(image, lines)
    assert len(cards) == 3
    assert [c.price for c in cards] == [None, 20500, 29500]
    assert "청바지" in cards[0].literal_title()


class Describer:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def message(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(next(self.responses), ensure_ascii=False)


def descriptions():
    return {
        "items": [
            {
                "card_id": i,
                "name": name,
                "category": "CLOTHING",
                "supplemental_question": "오늘 입을 상황이 있었나요?",
            }
            for i, name in enumerate(("코튼 패드 청바지", "데일리 후드 집업"))
        ]
    }


def run_cart(client, selected=(True, True, False)):
    image, lines = cart_fixture(selected)
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right - line.left}\t{line.bottom - line.top}\t95\t{line.text}"
        for line in lines
    )
    data = io.BytesIO()
    image.save(data, format="PNG")
    return AiService(Settings(_env_file=None), client=client, ocr=lambda image, settings: table).extract_cart(
        data.getvalue()
    )


def test_model_order_cannot_move_price_between_cards():
    response = descriptions()
    response["items"].reverse()
    client = Describer([response])
    result = run_cart(client)
    assert [i.name for i in result.items] == [i["name"] for i in descriptions()["items"]]
    assert [i.price for i in result.items] == [27500, 20500]
    # Model receives independent card descriptions, with no price matching task.
    assert "27500" not in client.calls[0]["content"][0]["text"]
    assert "레드 스티치" not in client.calls[0]["content"][0]["text"]


def test_no_selected_product_does_not_fall_back_to_extracting_all_or_call_ai():
    client = Describer([])
    result = run_cart(client, selected=(False, False, False))
    assert result.items == []
    assert "NO_SELECTED_ITEMS" in result.warnings
    assert client.calls == []


def test_only_middle_product_selected_keeps_its_own_name_and_price():
    response = descriptions()
    response["items"] = [response["items"][1]]
    response["items"][0]["card_id"] = 0
    result = run_cart(Describer([response]), selected=(False, True, False))
    assert [(i.name, i.price) for i in result.items] == [("데일리 후드 집업", 20500)]


def test_shifted_or_rewritten_model_name_keeps_own_literal_title_and_price():
    response = descriptions()
    response["items"][0]["name"] = "데일리 후드 집업"
    result = run_cart(Describer([response]))
    assert result.items[0].name == "코튼 패드 청바지"
    assert result.items[0].price == 27500
    assert result.items[0].category_source == "FALLBACK"
    assert "REVIEW_OCR_NAMES" in result.warnings


def test_small_ocr_typo_repair_is_allowed_only_in_the_matching_card():
    cards = [
        CartCard([Line(0, 0, 300, 30, "코튼 패드 청바지")], 27500),
        CartCard([Line(0, 0, 300, 30, "데일리 소매워머 후드 집업")], 20500),
        CartCard([Line(0, 0, 500, 30, "유니크 레드 스티치 포인트 힌 외이드 데님")], 29500),
    ]
    repaired = "유니크 레드 스티치 포인트 힙 와이드 데님"
    assert name_belongs_to_card(repaired, cards, 2)
    assert not name_belongs_to_card(repaired, cards, 0)
    assert not name_belongs_to_card("데일리 소매워머 후드 집업", cards, 0)
    assert not name_belongs_to_card("이름을 알 수 없는 새로운 상품", cards, 0)


@pytest.mark.parametrize("error", ["missing_card", "duplicate_card", "invented_price"])
def test_invalid_card_identity_is_retried_then_rejected(error):
    response = descriptions()
    if error == "missing_card":
        response["items"].pop(0)
    elif error == "duplicate_card":
        response["items"][0]["card_id"] = 1
    else:
        response["items"][0]["price"] = 61770
    client = Describer([response, response])
    result = run_cart(client)
    assert len(client.calls) == 2
    assert result.items == []
    assert "AI_INVALID_OUTPUT" in result.warnings
