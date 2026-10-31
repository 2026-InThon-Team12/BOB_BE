"""Vision-first AI requests retain the deployed cart evidence checks."""

import io
import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.lablup import LablupClient
from app.services.ai import AiService
from test_cart_layout import cart_fixture, descriptions


def run_cart(handler, *, legacy_flag=False):
    image, lines = cart_fixture()
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right-line.left}\t{line.bottom-line.top}\t95\t{line.text}"
        for line in lines
    )
    data = io.BytesIO()
    image.save(data, format="PNG")
    config = Settings(_env_file=None, lablup_api_key="test-only", lablup_vision_enabled=legacy_flag)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return AiService(
            config, client=LablupClient(config, client), ocr=lambda *_: table,
            price_reader=lambda *_: ["27,500원", "20,500원"],
        ).extract_cart(data.getvalue())


def success():
    return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(descriptions())}]})


@pytest.mark.parametrize("legacy_flag", [False, True])
def test_cart_ai_always_receives_images_and_keeps_verified_prices(legacy_flag):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return success()

    result = run_cart(handler, legacy_flag=legacy_flag)
    assert len(calls) == 1
    assert sum(b["type"] == "image" for b in calls[0]["messages"][0]["content"]) == 2
    assert result.analysis_method == "vision"
    assert [item.price for item in result.items] == [27500, 20500]
    assert result.selection_summary == {"visible": 3, "selected": 2, "excluded": 1}
    # Whole-row images sent to AI are not mislabeled as product-only thumbnails.
    assert all(item.image_crop is None for item in result.items)


@pytest.mark.parametrize("status,body", [
    (400, {"error": "invalid image dimensions"}),
    (415, {"error": "unsupported image format"}),
    (422, {"error": "invalid request schema"}),
    (401, {"error": "unauthorized"}),
    (429, {"error": "rate limit"}),
    (500, {"error": "server error"}),
])
def test_other_failures_never_retry_with_text_only(status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    result = run_cart(handler)
    assert len(calls) == 1
    assert result.analysis_method == "manual"
    assert result.items == []
    assert "VISION_UNAVAILABLE_USING_OCR" not in result.warnings


@pytest.mark.parametrize("error", [
    "image input unsupported",
    {"code": "vision_not_supported", "message": "not available"},
    {"message": "This model does not support images"},
])
def test_explicit_image_capability_error_retries_once_with_text(error):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": error})
        return success()

    result = run_cart(handler)
    assert len(calls) == 2
    assert all(b["type"] == "text" for b in calls[1]["messages"][0]["content"])
    assert result.analysis_method == "ocr"
    assert "VISION_UNAVAILABLE_USING_OCR" in result.warnings
    assert [item.price for item in result.items] == [27500, 20500]
