import io
import json
import subprocess

import httpx
import pytest
from PIL import Image

from app.core.config import Settings
from app.integrations.lablup import LablupClient, LablupError
from app.models import Category, ItemSource
from app.services.ai import AiService, ImageInputError, extract_ocr, validate_image


def settings(**overrides):
    return Settings(_env_file=None, lablup_api_key="test-secret-not-for-output", **overrides)


def png_bytes():
    with Image.new("RGB", (100, 100), "white") as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def proposal(**overrides):
    return {
        "items": [
            {
                "name": "키보드",
                "price": 30_000,
                "category": "DIGITAL",
                "supplemental_question": "오늘 키보드가 없어 불편했던 순간이 있었나요?",
                **overrides,
            }
        ]
    }


def response_for(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


def service(handler, **kwargs):
    config = settings(**kwargs)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AiService(config, client=LablupClient(config, client=client))


def test_manual_name_and_price_remain_authoritative():
    requests = []

    def handler(request):
        requests.append(request)
        return response_for(proposal(name="다른 제품", price=99_000))

    result = service(handler).draft_item("나의 키보드", 20_000)
    assert result.analysis_method == "text"
    assert result.items[0].name == "나의 키보드"
    assert result.items[0].price == 20_000
    assert result.items[0].category == Category.DIGITAL
    assert str(requests[0].url) == "https://api.console.lablup.ai/anthropic/v1/messages"
    assert requests[0].headers["x-api-key"] == "test-secret-not-for-output"
    assert json.loads(requests[0].content)["model"] == "DeepSeek-V4.1-Flash-B300"


def test_invalid_json_is_corrected_exactly_once():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response_for("not json" if len(calls) == 1 else proposal())

    result = service(handler).draft_item("키보드", 30_000)
    assert result.analysis_method == "text"
    assert len(calls) == 2
    assert "failed schema validation" in calls[1]["system"]


@pytest.mark.parametrize("change", [{"price": 1.5}, {"price": True}, {"category": "INVENTED"}])
def test_invalid_output_after_correction_uses_manual_fallback(change):
    calls = []

    def handler(request):
        calls.append(request)
        return response_for(proposal(**change))

    result = service(handler).draft_item("키보드", 30_000)
    assert len(calls) == 2
    assert result.analysis_method == "manual"
    assert result.items[0].category == Category.OTHER
    assert "AI_INVALID_OUTPUT" in result.warnings


def test_missing_credentials_keeps_direct_entry_working():
    result = AiService(Settings(_env_file=None, lablup_api_key=None)).draft_item("운동화", 50_000)
    assert result.items[0].price == 50_000
    assert result.items[0].source == ItemSource.MANUAL
    assert result.warnings == ["AI_NOT_CONFIGURED", "REVIEW_CATEGORY_MANUALLY"]


def test_ocr_path_sends_only_text_and_preserves_unknown_price():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response_for(proposal(price=None))

    ai = service(handler)
    ai.ocr = lambda image, config: "left\ttop\ttext\n0\t0\t키보드"
    result = ai.extract_cart(png_bytes())
    assert result.analysis_method == "ocr"
    assert result.items[0].price is None
    assert result.items[0].source == ItemSource.SCREENSHOT
    assert all(block["type"] == "text" for block in requests[0]["messages"][0]["content"])
    assert "CONFIRM_EXTRACTED_ITEMS" in result.warnings


def test_rejected_vision_request_falls_back_to_ocr():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if any(block["type"] == "image" for block in payload["messages"][0]["content"]):
            return httpx.Response(400, json={"error": "image input unsupported"})
        return response_for(proposal())

    ai = service(handler, lablup_vision_enabled=True)
    ai.ocr = lambda image, config: "키보드 30000"
    result = ai.extract_cart(png_bytes())
    assert result.analysis_method == "ocr"
    assert len(calls) == 2
    assert "VISION_UNAVAILABLE_USING_OCR" in result.warnings


def test_timeout_error_does_not_expose_upstream_details_or_secret():
    def handler(request):
        raise httpx.ReadTimeout("upstream said test-secret-not-for-output", request=request)

    result = service(handler).draft_item("키보드", 30_000)
    assert "AI_TIMEOUT" in result.warnings
    assert "test-secret" not in result.model_dump_json()


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg onload='alert(1)'/>"])
def test_image_bytes_are_verified(data):
    with pytest.raises(ImageInputError):
        validate_image(data, 10_000)


def test_image_size_limit():
    with pytest.raises(ImageInputError) as caught:
        validate_image(png_bytes(), 10)
    assert caught.value.status_code == 413


def test_missing_ocr_engine_returns_actionable_fallback(monkeypatch):
    def missing_engine(*args, **kwargs):
        raise FileNotFoundError("not installed")

    monkeypatch.setattr(subprocess, "run", missing_engine)
    result = AiService(settings()).extract_cart(png_bytes())
    assert result.items == []
    assert result.warnings == ["OCR_UNAVAILABLE", "ENTER_ITEMS_MANUALLY"]


def test_ocr_retains_word_positions_without_empty_hierarchy(monkeypatch):
    text = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t10\t20\t30\t40\t95\t키보드\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, text.encode(), b"")
    )
    with Image.new("RGB", (100, 100), "white") as image:
        result = extract_ocr(image, settings())
    assert "10\t20\t30\t40\t95\t키보드" in result
    assert "-1" not in result


def test_redirect_is_not_followed_and_does_not_expose_credentials():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(307, headers={"location": "https://example.invalid/steal"})

    config = settings()
    client = LablupClient(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LablupError, match="AI_UNAVAILABLE"):
        client.message(system="test", content=[{"type": "text", "text": "test"}])
    assert len(seen) == 1
