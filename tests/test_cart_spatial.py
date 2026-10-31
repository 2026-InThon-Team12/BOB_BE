import base64
import copy
import io
import json

import pytest
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.integrations.lablup import LablupError
from app.services.ai import AiService
from app.services.cart_geometry import selector_candidates
from app.services.cart_layout import CartCard
from app.services.cart_spatial import SpatialCart, verify_layout, recover_with_ocr_layout
from app.services.ocr_layout import Line


def fixture():
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    words, items = [], []
    for i, top in enumerate((100, 520)):
        draw.rectangle(
            (30, top, 80, top + 50), fill="#00aa55" if i == 0 else "white", outline="#aaaaaa", width=3
        )
        draw.rectangle((100, top + 70, 250, top + 220), fill="red" if i == 0 else "blue")
        name = "빨간 가방" if i == 0 else "파란 가방"
        words.extend(
            [
                Line(300, top + 70, 800, top + 100, name),
                Line(500, top + 200, 750, top + 240, f"{10000 + i * 2000:,}원"),
            ]
        )

        def box(x1, y1, x2, y2):
            return [x1, y1, x2, y2]

        items.append(
            dict(
                item_id=i,
                row=box(20, top - 10, 950, top + 350),
                control_id=i,
                selection="SELECTED" if i == 0 else "UNSELECTED",
                title=name,
                name=name,
                category="OTHER",
                title_box=box(295, top + 65, 805, top + 105),
                price=10000 + i * 2000,
                price_box=box(495, top + 195, 755, top + 245),
                photo_box=box(100, top + 70, 250, top + 220),
            )
        )
    controls = selector_candidates(image)
    for i, top in enumerate((100, 520)):
        items[i]["control_id"] = next(
            j for j, c in enumerate(controls) if abs(c.top - top) < 3 and c.left < 50
        )
    return image, words, {"items": items}, controls


def verify(change=None, reading="10,000원"):
    image, words, raw, controls = fixture()
    if change:
        change(raw["items"])
    proposal = SpatialCart.model_validate_json(json.dumps(raw))
    return verify_layout(
        image, words, proposal, controls, Settings(_env_file=None), lambda crops, _: [reading] * len(crops)
    )


def test_spatial_selection_price_and_photo_match_own_row():
    rows = verify()
    assert [r.selected for r in rows] == [True, False]
    assert rows[0].card.price == 10000 and rows[0].review_fields == []
    with Image.open(io.BytesIO(base64.b64decode(rows[0].crop["base64"]))) as photo:
        assert photo.getpixel((50, 50))[0] > 240


def test_disagreement_is_per_item_not_a_failure_of_the_whole_cart():
    rows = verify(lambda items: items[1].update(selection="SELECTED"))
    assert rows[0].selected is True
    assert rows[1].selected is None and "selection" in rows[1].review_fields


@pytest.mark.parametrize("reading", ["", "12,000원", "10,000원 20,000원"])
def test_independent_price_reading_never_replaces_the_price(reading):
    row = verify(reading=reading)[0]
    assert row.selected is True and row.card.price is None
    assert row.crop and "price" in row.review_fields


def test_model_cannot_invent_a_price_without_local_ocr_evidence():
    assert verify(lambda items: items[0].update(price=99999))[0].card.price is None


def test_approximate_price_box_snaps_to_intersecting_ocr_line():
    row = verify(lambda items: items[0].update(price_box=[450, 290, 550, 345]))[0]
    assert row.card.price == 10000
    assert row.card.price_evidence.left == 500 and row.card.price_evidence.right == 750


def test_correct_amount_elsewhere_in_row_cannot_rescue_wrong_price_region():
    row = verify(lambda items: items[0].update(price_box=[450, 400, 550, 440]))[0]
    assert row.card.price is None


def test_photo_ocr_noise_is_not_joined_to_adjacent_price():
    image, words, raw, controls = fixture()
    words[1] = Line(300, 300, 750, 340, "10,000원")
    words.append(Line(120, 300, 240, 340, "99,999원"))
    raw["items"][0].update(price_box=[295, 295, 755, 345], photo_box=[100, 170, 250, 350])
    rows = verify_layout(
        image,
        words,
        SpatialCart.model_validate_json(json.dumps(raw)),
        controls,
        Settings(_env_file=None),
        lambda *_: ["10,000원"],
    )
    assert rows[0].card.price == 10000
    assert rows[0].card.price_evidence.left == 300


def test_photo_from_next_row_is_dropped_without_dropping_valid_price():
    rows = verify(lambda items: items[0].update(photo_box=items[1]["photo_box"]))
    assert rows[0].selected is True and rows[0].card.price == 10000
    assert rows[0].crop is None and "photo" in rows[0].review_fields


def test_duplicate_control_never_uses_first_wins():
    rows = verify(lambda items: items[1].update(control_id=items[0]["control_id"]))
    assert all(row.selected is None for row in rows)


def test_overlapping_rows_require_confirmation():
    rows = verify(lambda items: items[0]["row"].__setitem__(3, 900))
    assert all(row.selected is None for row in rows)


def test_omitting_next_product_does_not_allow_a_row_to_swallow_its_checkbox():
    def merged(items):
        items.pop()
        items[0]["row"][3] = 900

    rows = verify(merged)
    assert rows[0].selected is None


def test_ai_order_does_not_change_output_order_or_pairing():
    rows = verify(lambda items: items.reverse())
    assert [r.name for r in rows] == ["빨간 가방", "파란 가방"]


def test_unknown_selection_is_separate_from_automatic_drafts(monkeypatch):
    monkeypatch.setattr(AiService, "_complete_ocr_cart", lambda *_: None)
    # An ambiguous OCR layout cannot resolve the disputed AI selection.
    monkeypatch.setattr("app.services.cart_spatial.checkbox_cards", lambda *_: [])
    image, words, raw, _ = fixture()
    raw["items"][1]["selection"] = "SELECTED"
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{w.left}\t{w.top}\t{w.right - w.left}\t{w.bottom - w.top}\t90\t{w.text}" for w in words
    )

    class Client:
        def message(self, **kwargs):
            assert kwargs["content"][0]["type"] == "image"
            return json.dumps(copy.deepcopy(raw))

    data = io.BytesIO()
    image.save(data, format="PNG")
    result = AiService(
        Settings(_env_file=None, cart_layout_vision_enabled=True, lablup_vision_enabled=True),
        client=Client(),
        ocr=lambda *_: table,
        price_reader=lambda *_: ["10,000원", "12,000원"],
    ).extract_cart(data.getvalue())
    assert result.extraction_strategy == "spatial_ai"
    assert [item.name for item in result.items] == ["빨간 가방"]
    assert [item.name for item in result.review_items] == ["파란 가방"]
    assert result.selection_summary == {"visible": 2, "selected": 1, "excluded": 0, "unknown": 1}
    assert "PARTIAL_SELECTION_UNVERIFIED" in result.warnings


def test_independent_ocr_layout_recovers_invalid_ai_row_without_borrowing_next_item():
    image, words, raw, controls = fixture()
    raw["items"][0]["row"][3] = 600  # overlaps another product, not trusted as an AI row
    rows = verify_layout(
        image,
        words,
        SpatialCart.model_validate_json(json.dumps(raw)),
        controls,
        Settings(_env_file=None),
        lambda crops, _: ["10,000원"] * len(crops),
    )
    assert rows[0].selected is None
    recover_with_ocr_layout(
        image, words, rows, Settings(_env_file=None), lambda crops, _: ["10,000원"] * len(crops)
    )
    assert rows[0].selected is True and rows[0].card.price == 10000
    assert rows[1].selected is False
    assert "OCR_LAYOUT_RECOVERED" in rows[0].verification_notes


def test_ocr_recovery_rejects_title_from_another_region():
    image, words, raw, controls = fixture()
    raw["items"][0]["control_id"] = None
    raw["items"][0]["title"] = "파란 가방"
    rows = verify_layout(
        image,
        words,
        SpatialCart.model_validate_json(json.dumps(raw)),
        controls,
        Settings(_env_file=None),
        lambda crops, _: ["10,000원"] * len(crops),
    )
    recover_with_ocr_layout(
        image, words, rows, Settings(_env_file=None), lambda crops, _: ["10,000원"] * len(crops)
    )
    assert rows[0].selected is None and "selection" in rows[0].review_fields


def test_recovery_handles_unchecked_card_without_ocr_title(monkeypatch):
    image, words, _, _ = fixture()
    rows = verify(lambda items: items[0].update(control_id=None))
    cards = [
        CartCard(words[:1], 10000, True, (20, 90, 950, 450), words[1]),
        CartCard([], None, False, (20, 510, 950, 870)),
    ]
    monkeypatch.setattr("app.services.cart_spatial.checkbox_cards", lambda *_: cards)
    recover_with_ocr_layout(
        image, words, rows, Settings(_env_file=None), lambda crops, _: ["10,000원"] * len(crops)
    )
    assert rows[0].selected is True and rows[0].card.price == 10000
    assert rows[1].selected is False


def test_recovery_uses_same_physical_control_with_partially_read_title():
    image, words, _, _ = fixture()

    def ambiguous(items):
        items[0]["title"] = "빨간 가방 크기"
        items[0]["price_box"] = [850, 300, 940, 340]

    rows = verify(ambiguous)
    assert rows[0].selected is True and rows[0].card.price is None
    assert "name" not in rows[0].review_fields
    recover_with_ocr_layout(
        image, words, rows, Settings(_env_file=None), lambda crops, _: ["10,000원"] * len(crops)
    )
    assert rows[0].card.price == 10000


@pytest.mark.parametrize("failure", ["AI_TIMEOUT", "AI_OUTPUT_LIMIT", "invalid_json"])
def test_failed_layout_preserves_verified_ocr_fallback_and_reports_cause(failure, monkeypatch):
    monkeypatch.setattr(AiService, "_complete_ocr_cart", lambda *_: None)
    image, words, _, _ = fixture()
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{w.left}\t{w.top}\t{w.right - w.left}\t{w.bottom - w.top}\t90\t{w.text}" for w in words
    )

    class Client:
        def message(self, **kwargs):
            if kwargs["content"][0]["type"] == "text":
                cards = json.loads(kwargs["content"][0]["text"])
                return json.dumps(
                    {
                        "items": [
                            {
                                "card_id": c["card_id"],
                                "name": c["source_title"],
                                "display_name": "빨간 가방",
                                "category": "OTHER",
                                "supplemental_question": "오늘 가방이 필요했나요?",
                            }
                            for c in cards
                        ]
                    }
                )
            if failure == "invalid_json":
                return "{invalid"
            raise LablupError(failure)

    data = io.BytesIO()
    image.save(data, format="PNG")
    result = AiService(
        Settings(_env_file=None, cart_layout_vision_enabled=True, lablup_vision_enabled=True),
        client=Client(),
        ocr=lambda *_: table,
        price_reader=lambda crops, _: ["10,000원"] * len(crops),
    ).extract_cart(data.getvalue())
    assert result.extraction_strategy == "spatial_ocr"
    assert "SPATIAL_LAYOUT_UNAVAILABLE_USING_OCR" in result.warnings
    assert ("CART_LAYOUT_INVALID" if failure == "invalid_json" else failure) in result.warnings
    assert [item.price for item in result.items] == [10000]
    assert result.selection_summary["excluded"] == 1
    assert result.review_items == []


def test_complete_local_evidence_skips_full_image_ai_and_survives_naming_timeout():
    image, words, _, _ = fixture()
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{w.left}\t{w.top}\t{w.right - w.left}\t{w.bottom - w.top}\t90\t{w.text}" for w in words
    )

    class Client:
        def message(self, **kwargs):
            # Crop-based naming starts with card text; whole-cart analysis starts
            # with the full screenshot and must not run on complete local evidence.
            assert kwargs["content"][0]["type"] == "text"
            raise LablupError("AI_TIMEOUT")

    data = io.BytesIO()
    image.save(data, format="PNG")
    result = AiService(
        Settings(_env_file=None, cart_layout_vision_enabled=True, lablup_vision_enabled=True),
        client=Client(),
        ocr=lambda *_: table,
        price_reader=lambda crops, _: ["10,000원"] * len(crops),
    ).extract_cart(data.getvalue())
    assert result.extraction_strategy == "spatial_ocr"
    assert [item.price for item in result.items] == [10000]
    assert result.items[0].image_crop is not None
    assert result.items[0].review_fields == ["name"]
    assert result.selection_summary["excluded"] == 1
    assert "AI_TIMEOUT" in result.warnings
