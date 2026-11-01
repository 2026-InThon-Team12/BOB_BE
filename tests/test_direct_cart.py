import base64
import io
import json
import math

import httpx
import pytest
from PIL import Image

from app.core.config import Settings
from app.integrations.ai_provider import create_ai_client
from app.models import Category
from app.services.ai import AiService
from app.services.direct_cart import original_box, vision_size


def run_cart(rows=None, *, reply=None, status=200, vision=True, provider="claude", output_mode="schema"):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            status,
            json={
                "content": [{"type": "text", "text": reply if reply is not None else json.dumps({"items": rows})}],
                "stop_reason": "end_turn",
            },
        )

    def no_ocr(*_):
        pytest.fail("Direct vision must never call OCR or price re-verification, even on failure")

    settings = Settings(
        _env_file=None, ai_provider=provider,
        claude_api_key="fake-test-key", claude_vision_enabled=vision,
        lablup_api_key="fake-lablup-key", lablup_vision_enabled=vision, lablup_cart_mode="direct",
        direct_cart_output_mode=output_mode,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = create_ai_client(settings, http)
        with Image.new("RGB", (1000, 2000), "red") as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        result = AiService(settings, client, ocr=no_ocr, price_reader=no_ocr).extract_cart(buffer.getvalue())
        assert client.deadline is None
    return result, calls


def row(**overrides):
    return {"selected": True, "name": "상품", "price": 15900, "photo_box": [78, 157, 392, 784], **overrides}


def test_direct_cart_one_request_four_fields_without_ocr():
    result, calls = run_cart([
        row(name="빨간 가방"),
        row(selected=False, name="선택하지 않은 상품"),
        row(selected=None, name="확인이 필요한 상품"),
    ])
    assert len(calls) == 1
    assert result.extraction_strategy == "direct_vision"
    assert result.selection_summary == {"visible": 3, "selected": 1, "excluded": 1, "unknown": 1}
    assert [item.name for item in result.items] == ["빨간 가방"]
    assert result.review_items[0].review_fields == ["selection"]
    item = result.items[0]
    assert item.price == 15900
    assert item.category == Category.OTHER and item.category_source == "FALLBACK"
    assert item.supplemental_question
    assert item.image_crop["box"] == [99, 100, 500, 500]
    with Image.open(io.BytesIO(base64.b64decode(item.image_crop["base64"]))) as crop:
        assert crop.size == (160, 320)
        assert crop.getpixel((20, 20))[0] > 240
    payload = calls[0]
    assert payload["output_config"]["effort"] == "low"
    schema = payload["output_config"]["format"]["schema"]
    assert set(schema["properties"]["items"]["items"]["properties"]) == {"selected", "name", "price", "photo_box"}
    content = payload["messages"][0]["content"]
    assert sum(block["type"] == "image" for block in content) == 1
    with Image.open(io.BytesIO(base64.b64decode(content[0]["source"]["data"]))) as sent:
        assert sent.size == (784, 1568)


@pytest.mark.parametrize("size", [(4000, 4000), (1920, 1080), (1080, 2504), (100, 100), (1, 9000)])
def test_resize_preserves_bounded_coordinate_system(size):
    sent = vision_size(size)
    assert 0 < sent[0] <= min(1568, size[0]) and 0 < sent[1] <= min(1568, size[1])
    assert math.ceil(sent[0] / 28) * math.ceil(sent[1] / 28) <= 1568
    assert original_box([0, 0, *sent], sent, size) == (0, 0, *size)


@pytest.mark.parametrize("box", [None, [], [0, 1, 2], [-1, 0, 20, 20], [20, 20, 10, 10], [0, 0, 9999, 20]])
def test_invalid_photo_keeps_name_price_and_other_items(box):
    result, _ = run_cart([row(photo_box=box), row(name="다른 상품")])
    assert len(result.items) == 2
    assert result.items[0].image_crop is None
    assert result.items[0].price == 15900
    assert result.items[0].review_fields == ["photo"]
    assert result.items[1].image_crop is not None


@pytest.mark.parametrize("price", [None, 0, -1, 1_000_000_000_001])
def test_uncertain_price_or_name_keeps_crop(price):
    result, _ = run_cart([row(name=None, price=price)])
    assert result.items[0].price is None
    assert result.items[0].name == "이름 확인 필요"
    assert result.items[0].review_fields == ["name", "price"]
    assert result.items[0].image_crop is not None


@pytest.mark.parametrize("kwargs,code", [
    ({"status": 400}, "AI_REQUEST_REJECTED"),
    ({"status": 429}, "AI_UNAVAILABLE"),
    ({"reply": "not JSON"}, "AI_INVALID_OUTPUT"),
    ({"reply": '{"items":[{"selected":"true","name":"x","price":1,"photo_box":null}]}'}, "AI_INVALID_OUTPUT"),
])
@pytest.mark.parametrize("provider", ["lablup", "claude"])
def test_failure_has_no_retry_or_ocr_fallback(kwargs, code, provider):
    result, calls = run_cart([], provider=provider, **kwargs)
    assert result.analysis_method == "manual" and not result.items
    assert code in result.warnings
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["lablup", "claude"])
def test_direct_vision_flag_does_not_trigger_hidden_ocr(provider):
    result, calls = run_cart([], vision=False, provider=provider)
    assert "VISION_UNAVAILABLE" in result.warnings
    assert not calls


def test_too_many_selected_are_not_silently_truncated():
    result, _ = run_cart([row()] * 21)
    assert not result.items
    assert "TOO_MANY_SELECTED_ITEMS" in result.warnings
    assert result.selection_summary["selected"] == 21


def test_all_unchecked_is_valid_empty_selection():
    result, _ = run_cart([row(selected=False)])
    assert not result.items and not result.review_items
    assert "NO_SELECTED_ITEMS" in result.warnings


@pytest.mark.parametrize("output_mode", ["schema", "prompt"])
def test_both_direct_providers_send_identical_images_prompt_schema_and_limits(output_mode):
    outputs = {}
    for provider in ("lablup", "claude"):
        result, calls = run_cart([row(), row(selected=False)], provider=provider, output_mode=output_mode)
        assert len(calls) == 1
        assert result.selection_summary == {"visible": 2, "selected": 1, "excluded": 1, "unknown": 0}
        assert result.items[0].verification_notes == [f"{provider.upper()}_DIRECT_VISION"]
        payload = calls[0]
        outputs[provider] = {
            key: payload[key] for key in ("system", "messages", "max_tokens")
        } | {"format": payload.get("output_config", {}).get("format")}
        if output_mode == "prompt":
            assert outputs[provider]["format"] is None
            assert "Required JSON schema:" in payload["system"]
    assert outputs["lablup"] == outputs["claude"]


def test_old_lablup_mode_remains_default_and_provider_switch_selects_correct_mode():
    settings = Settings(_env_file=None)
    assert settings.ai_cart_mode == "verified"
    assert settings.model_copy(update={"ai_provider": "claude"}).ai_cart_mode == "direct"
    assert settings.model_copy(update={"lablup_cart_mode": "direct"}).ai_cart_mode == "direct"
