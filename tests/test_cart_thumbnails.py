import base64
import io
import json

from PIL import Image, ImageDraw

from app.core.config import Settings
from app.services.ai import AiService
from app.services.cart_thumbnails import encode_thumbnail, thumbnail_box
from app.services.cart_layout import CartCard
from app.services.ocr_layout import Line


def test_crop_stays_inside_own_card_and_encodes_original_pixels():
    image = Image.new("RGB", (800, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 300, 300), fill="red")
    draw.rectangle((100, 550, 300, 750), fill="blue")
    card = CartCard([], 10000, True, (0, 0, 800, 450))
    box = thumbnail_box(image, card, [])
    assert box and box[3] < 450
    encoded = encode_thumbnail(image, box)
    with Image.open(io.BytesIO(base64.b64decode(encoded["base64"]))) as crop:
        assert max(crop.size) <= 320
        r, g, b = crop.getpixel((crop.width // 2, crop.height // 2))
        assert r > 240 and g < 10 and b < 10


def test_text_only_or_two_ambiguous_photos_are_not_used():
    image = Image.new("RGB", (800, 450), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 300, 300), fill="black")
    card = CartCard([], None, True, (0, 0, 800, 450))
    assert thumbnail_box(image, card, [Line(95, 95, 305, 305, "product title")]) is None
    draw.rectangle((450, 100, 650, 300), fill="blue")
    assert thumbnail_box(image, card, []) is None


def test_ocr_returns_matching_photos_for_selected_cards_only():
    from test_cart_layout import cart_fixture, descriptions

    image, lines = cart_fixture()
    draw = ImageDraw.Draw(image)
    for y, color in [(420, "red"), (1030, "blue"), (1640, "green")]:
        draw.rectangle((140, y, 340, y + 200), fill=color)
    # A whole photograph misread as one tall glyph must not erase the photo.
    lines += [Line(145, 425, 335, 615, "i")]
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right - line.left}\t{line.bottom - line.top}\t90\t{line.text}"
        for line in lines
    )

    class Fake:
        def message(self, **kwargs):
            return json.dumps(descriptions(), ensure_ascii=False)

    data = io.BytesIO()
    image.save(data, format="PNG")
    result = AiService(
        Settings(_env_file=None),
        client=Fake(),
        ocr=lambda *_: table,
        price_reader=lambda *_: ["27,500원", "20,500원"],
    ).extract_cart(data.getvalue())
    assert len(result.items) == 2
    assert [i.price for i in result.items] == [27500, 20500]
    assert "PRODUCT_IMAGE_UNAVAILABLE" not in result.warnings
    for item, channel in zip(result.items, (0, 2)):
        with Image.open(io.BytesIO(base64.b64decode(item.image_crop["base64"]))) as crop:
            pixel = crop.getpixel((crop.width // 2, crop.height // 2))
            assert pixel[channel] > 240
