"""Merchant-independent layout matrix; only synthetic screens are used."""

import pytest
from PIL import Image, ImageDraw

from app.services.cart_layout import checkbox_cards, card_price_values
from app.services.ocr_layout import Line


def screen(shape="square", side="left", dark=False, columns=1, scale=1):
    w, h = 1080, 1100
    background = "#171717" if dark else "white"
    image = Image.new("RGB", (w, h), background)
    draw = ImageDraw.Draw(image)
    lines = []
    for col in range(columns):
        start = col * w // columns
        cw = w // columns
        for row in range(2):
            top = 80 + row * 450
            x = start + 24 if side == "left" else start + cw - 74
            selected = row == col % 2
            box = (x, top, x + 48, top + 48)
            paint = draw.ellipse if shape == "circle" else draw.rectangle
            paint(
                box,
                fill="#ff477e" if selected else background,
                outline="#bbbbbb" if dark else "#999999",
                width=3,
            )
            if selected:
                draw.line((x + 10, top + 25, x + 21, top + 36, x + 39, top + 12), fill="white", width=5)
            title_left = start + 100 if side == "left" else start + 25
            title_top = top if columns == 1 else top + 140
            lines += [
                Line(title_left, title_top, title_left + 230, title_top + 25, f"canvas backpack {col} {row}"),
                Line(
                    start + 110,
                    top + 250,
                    start + 330,
                    top + 280,
                    f"판매가 {19000 + col * 1000 + row * 100}원",
                ),
            ]
    if scale != 1:
        image = image.resize((int(w * scale), int(h * scale)))
        lines = [
            Line(*(round(v * scale) for v in (line.left, line.top, line.right, line.bottom)), line.text)
            for line in lines
        ]
    return image, lines


@pytest.mark.parametrize("shape", ["square", "circle"])
@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [0.5, 1, 1.5])
def test_selector_styles_themes_positions_and_resolutions(shape, side, dark, scale):
    image, lines = screen(shape, side, dark, scale=scale)
    cards = checkbox_cards(image, lines)
    assert [(c.selected, c.price) for c in cards] == [(True, 19000), (False, 19100)]


@pytest.mark.parametrize("shape", ["square", "circle"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_two_column_grid_does_not_pair_prices_across_columns(shape, side):
    image, lines = screen(shape, side, columns=2)
    cards = checkbox_cards(image, lines)
    assert len(cards) == 4
    assert [c.price for c in cards if c.selected] == [19000, 20100]
    assert [c.literal_title() for c in cards if c.selected] == ["canvas backpack 0 0", "canvas backpack 1 1"]


def test_single_item_has_no_requirement_for_a_second_checkbox():
    image, lines = screen()
    image = image.crop((0, 0, 1080, 450))
    cards = checkbox_cards(image, [line for line in lines if line.top < 450])
    assert len(cards) == 1 and cards[0].price == 19000 and cards[0].selected is True


def test_single_row_grid_keeps_each_column_separate():
    image, lines = screen(columns=2)
    cards = checkbox_cards(image.crop((0, 0, 1080, 450)), [line for line in lines if line.top < 450])
    assert [(c.selected, c.price) for c in cards] == [(True, 19000), (False, 20000)]


def test_uncertain_mark_is_not_assumed_checked():
    image, lines = screen()
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 80, 72, 128), fill="white", outline="#999999", width=3)
    draw.rectangle((46, 102, 50, 106), fill="black")
    cards = checkbox_cards(image, lines)
    assert cards[0].selected is None


def test_checkbox_ocr_glyph_is_removed_before_grouping_title():
    image, words = screen(shape="circle", dark=True)
    # Sparse OCR can read a filled round control as @ on the title baseline.
    words.append(Line(24, 80, 73, 129, "@"))
    cards = checkbox_cards(image, words, words)
    assert [(c.selected, c.price) for c in cards] == [(True, 19000), (False, 19100)]
    assert "@" not in cards[0].literal_title()


def test_currency_glyph_missing_from_ocr_is_not_a_second_selector_column():
    image, words = screen(side="right")
    draw = ImageDraw.Draw(image)
    for y in (330, 780):
        # An OCR-missed won glyph looks like a small repeated square.
        draw.rectangle((335, y, 364, y + 29), fill="black")
    cards = checkbox_cards(image, words, words)
    assert [(c.selected, c.price) for c in cards] == [(True, 19000), (False, 19100)]


@pytest.mark.parametrize("outline", ["#e8ecef", "#eeeeee", "#f0f0f0"])
@pytest.mark.parametrize("scale", [0.5, 1, 1.5])
def test_pale_unchecked_borders_are_retained_with_colored_selected_control(outline, scale):
    image, words = screen()
    draw = ImageDraw.Draw(image)
    draw.rectangle((22, 528, 74, 580), fill="white")
    draw.rounded_rectangle((24, 530, 72, 578), radius=7, fill="white", outline=outline, width=4)
    # Repeated small badge shapes are distractions, not missing product controls.
    for y in (155, 605):
        draw.rectangle((500, y, 516, y + 16), fill="#00aa55")
    if scale != 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)))
        words = [Line(*(round(v * scale) for v in (w.left, w.top, w.right, w.bottom)), w.text) for w in words]
    cards = checkbox_cards(image, words, words)
    assert [(c.selected, c.price) for c in cards] == [(True, 19000), (False, 19100)]


def test_unchecked_row_without_readable_title_is_counted_but_never_imported():
    image, words = screen()
    words = [word for word in words if word.top < 500 or "판매가" in word.text]
    cards = checkbox_cards(image, words, words)
    assert len(cards) == 2
    assert cards[0].selected is True
    assert cards[1].selected is False
    assert not cards[1].lines


@pytest.mark.parametrize(
    "text",
    [
        "USD 19,000",
        "$19,000",
        "EUR 19,000",
        "월 할부 10,000원",
        "12개월 10,000원",
        "회원가 10,000원",
        "100g당 1,000원",
        "배송비 3,000원",
        "최대 혜택가 10,000원",
    ],
)
def test_non_item_or_conditional_prices_are_not_silently_used(text):
    assert card_price_values(text) == set()
