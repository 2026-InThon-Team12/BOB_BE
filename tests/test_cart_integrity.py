"""Cross-layout identity checks with synthetic pixels, no uploaded user data."""

import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.services.ai import AiService
from app.services.cart_layout import checkbox_cards
from app.services.cart_thumbnails import thumbnail_box
from app.services.ocr_layout import Line
from test_cart_variants import screen


def test_grid_header_in_another_column_cannot_hide_a_product():
    image, words = screen(columns=2)
    words.append(Line(650, 82, 850, 108, "전체선택"))
    cards = checkbox_cards(image, words, words)
    left = [card for card in cards if card.box[0] == 0]
    assert [(card.selected, card.price) for card in left] == [(True, 19000), (False, 19100)]


def test_inline_coupon_button_does_not_hide_a_real_product():
    image, words = screen()
    words.append(Line(800, 82, 1000, 108, "쿠폰받기"))
    cards = checkbox_cards(image, words, words)
    assert [(card.selected, card.price) for card in cards] == [(True, 19000), (False, 19100)]


def test_photo_search_stops_before_checkout_recommendations():
    image, words = screen()
    image = image.crop((0, 0, 1080, 500))
    words = [word for word in words if word.top < 450]
    words.append(Line(100, 380, 450, 405, "예상 주문금액"))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 410, 300, 490), fill="red")
    cards = checkbox_cards(image, words, words)
    assert len(cards) == 1
    assert cards[0].box[3] <= 380
    assert thumbnail_box(image, cards[0], words) is None


def test_ocr_line_crossing_product_boundary_cannot_supply_price():
    image, words = screen()
    words = [word for word in words if word.top != 330]
    words.append(Line(110, 510, 330, 560, "19,000원"))
    cards = checkbox_cards(image, words, words)
    assert cards[0].price is None


@pytest.mark.parametrize("columns", [1, 2, 3])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("disagree", [False, True])
def test_selected_name_price_photo_remain_together_through_ai(columns, dark, side, disagree):
    image, words = screen(columns=columns, dark=dark, side=side)
    draw = ImageDraw.Draw(image)
    colors = ["red", "blue", "green"]
    # Each product has a different colour; even unchecked products have photos.
    for col in range(columns):
        for row in range(2):
            left = col * 1080 // columns + 105
            top = 80 + row * 450 + 50
            draw.rectangle((left, top, left + 160, top + 95), fill=colors[(col + row) % 3])
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{w.left}\t{w.top}\t{w.right - w.left}\t{w.bottom - w.top}\t95\t{w.text}" for w in words
    )
    expected = sorted([(col % 2, col) for col in range(columns)])
    expected_prices = [19000 + col * 1000 + row * 100 for row, col in expected]

    class ReorderedAI:
        def message(self, **kwargs):
            sources = json.loads(kwargs["content"][0]["text"])
            assert [s["source_title"] for s in sources] == [
                f"canvas backpack {col} {row}" for row, col in expected
            ]
            assert sum(part["type"] == "image" for part in kwargs["content"]) == columns
            sent_photos = [part for part in kwargs["content"] if part["type"] == "image"]
            for photo, (row, col) in zip(sent_photos, expected):
                with Image.open(io.BytesIO(base64.b64decode(photo["source"]["data"]))) as crop:
                    pixel = crop.getpixel((crop.width // 2, crop.height // 2))
                    channel = [0, 2, 1][(row + col) % 3]
                    assert pixel[channel] > 100 and sum(pixel) - pixel[channel] < 25
            return json.dumps(
                {
                    "items": [
                        {
                            "card_id": s["card_id"],
                            "name": s["source_title"],
                            "display_name": f"가방 {s['card_id']}",
                            "category": "OTHER",
                            "supplemental_question": "오늘 사용할 일이 있었나요?",
                        }
                        for s in reversed(sources)
                    ]
                }
            )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result = AiService(
        Settings(_env_file=None, lablup_vision_enabled=True),
        client=ReorderedAI(),
        ocr=lambda *_: table,
        price_reader=lambda *_: [
            f"{price + (1 if disagree and i == 0 else 0)}원" for i, price in enumerate(expected_prices)
        ],
    ).extract_cart(buffer.getvalue())
    assert result.selection_summary == {"visible": columns * 2, "selected": columns, "excluded": columns}
    assert [item.name for item in result.items] == [f"가방 {i}" for i in range(columns)]
    assert [item.price for item in result.items] == [
        None if disagree and i == 0 else price for i, price in enumerate(expected_prices)
    ]
    for item, (row, col) in zip(result.items, expected):
        assert item.image_crop is not None
        with Image.open(io.BytesIO(base64.b64decode(item.image_crop["base64"]))) as crop:
            pixel = crop.getpixel((crop.width // 2, crop.height // 2))
            channel = [0, 2, 1][(row + col) % 3]
            assert pixel[channel] > 100 and sum(pixel) - pixel[channel] < 25
