import pytest
from PIL import Image

from app.core.config import Settings
from app.services.cart_layout import CartCard
from app.services.ocr_layout import Line
from app.services.price_evidence import verify_prices


@pytest.mark.parametrize(
    "reading,expected",
    [
        ("27,500원", 27500),
        ("27.500원", 27500),
        ("판매가 27.5002!", 27500),
        ("27,900원", None),
        ("", None),
        ("27,500원 30,000원", None),
    ],
)
def test_second_reading_must_agree_instead_of_replacing_the_first(reading, expected):
    card = CartCard(
        [Line(20, 20, 180, 45, "test coat")],
        27500,
        True,
        (0, 0, 300, 300),
        Line(20, 150, 200, 180, "27,500원"),
    )

    def reader(crops, settings):
        assert len(crops) == 1
        assert crops[0].height < 300  # only the price region is read
        return [reading]

    changed = verify_prices(Image.new("RGB", (300, 300), "white"), [card], Settings(_env_file=None), reader)
    assert card.price == expected
    assert changed == (expected is None)


def test_reader_failure_cannot_leave_an_unverified_price():
    card = CartCard([], 19000, True, None, Line(10, 10, 150, 40, "19,000원"))
    assert verify_prices(
        Image.new("RGB", (200, 100), "white"), [card], Settings(_env_file=None), lambda *_: []
    )
    assert card.price is None
