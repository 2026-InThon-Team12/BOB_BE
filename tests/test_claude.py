import io
import json
import logging
from datetime import timedelta

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.core.config import Settings
from app.integrations.ai_provider import create_ai_client
from app.integrations.claude import ClaudeClient
from app.integrations.lablup import LablupClient, LablupError
from app.services.ai import AiService
from app.services.discounts import extract_discount
from app.services.local_questions import LocalSummaryRequest, summarize_local
from app.services.pre_purchase_questions import PreQuestionAI
from app.services.purchase_questions import QuestionAI
from app.models import SimulationQuestion
from scripts.check_ai import probe


def config(**overrides):
    return Settings(
        _env_file=None,
        **{
            "ai_provider": "claude",
            "claude_api_key": "claude-test-secret",
            "lablup_api_key": "lablup-test-secret",
            **overrides,
        },
    )


def response(value="OK", **body):
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": value}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 12},
            **body,
        },
    )


def client_for(handler, **overrides):
    return create_ai_client(config(**overrides), httpx.Client(transport=httpx.MockTransport(handler)))


def png_bytes():
    with Image.new("RGB", (100, 100), "white") as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def test_direct_claude_uses_only_claude_credentials_and_compatible_thinking(caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    client = client_for(handler)
    with caplog.at_level(logging.INFO, logger="bob.ai"):
        assert client.message(system="private shopping context", content=[], thinking=False) == "OK"
    assert str(calls[0].url) == "https://api.anthropic.com/v1/messages"
    assert calls[0].headers["x-api-key"] == "claude-test-secret"
    payload = json.loads(calls[0].content)
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["thinking"] == {"type": "between_tools"}
    assert payload["output_config"] == {"effort": "low"}
    assert client.last_metrics["input_tokens"] == 50
    assert client.last_metrics["output_tokens"] == 12
    assert client.last_metrics["elapsed_ms"] >= 0
    for private in ("claude-test-secret", "lablup-test-secret", "private shopping context"):
        assert private not in caplog.text
        assert private not in repr(config())


def test_missing_claude_key_never_borrows_lablup_key():
    client = client_for(lambda _: pytest.fail("No request without a Claude key"), claude_api_key=None)
    with pytest.raises(LablupError, match="AI_NOT_CONFIGURED"):
        client.message(system="test", content=[])


def test_anthropic_key_environment_alias(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "alias-test-secret")
    settings = Settings(_env_file=None, ai_provider="claude")
    assert settings.claude_api_key == "alias-test-secret"
    assert "alias-test-secret" not in repr(settings)


def test_explicit_lablup_switch_keeps_original_endpoint_and_key():
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    client = client_for(handler, ai_provider="lablup")
    assert type(client) is LablupClient
    client.message(system="test", content=[], thinking=False)
    assert str(calls[0].url) == "https://api.console.lablup.ai/anthropic/v1/messages"
    assert calls[0].headers["x-api-key"] == "lablup-test-secret"
    assert json.loads(calls[0].content)["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("mode", ["disabled", "adaptive", "default"])
def test_other_model_options_are_explicit_and_do_not_inherit_lablup(mode):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response()

    client_for(handler, claude_thinking_mode=mode, claude_effort="default").message(
        system="test",
        content=[],
        thinking=False,
    )
    assert calls[0].get("thinking") == (None if mode == "default" else {"type": mode})
    assert "output_config" not in calls[0]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AI_AUTH_FAILED"),
        (403, "AI_AUTH_FAILED"),
        (404, "AI_MODEL_UNAVAILABLE"),
        (429, "AI_UNAVAILABLE"),
        (529, "AI_UNAVAILABLE"),
    ],
)
def test_failures_are_bounded_and_safe(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "private upstream text"}})

    with pytest.raises(LablupError, match=code) as caught:
        client_for(handler).message(system="test", content=[])
    assert len(calls) == 1
    assert "private" not in str(caught.value)


def test_option_error_is_not_mistaken_for_unsupported_images():
    client = client_for(
        lambda _: httpx.Response(
            400,
            json={
                "error": {"message": "thinking.type: disabled is not supported"},
            },
        )
    )
    with pytest.raises(LablupError) as caught:
        client.message(system="test", content=[{"type": "image"}])
    assert not caught.value.image_unsupported


def test_real_image_unsupported_error_can_use_ocr_fallback():
    client = client_for(
        lambda _: httpx.Response(
            400,
            json={
                "error": {"message": "This model does not support image input"},
            },
        )
    )
    with pytest.raises(LablupError) as caught:
        client.message(system="test", content=[{"type": "image"}])
    assert caught.value.image_unsupported


def test_cart_deadline_applies_to_claude_without_starting_request(monkeypatch):
    client = client_for(lambda _: pytest.fail("Expired budget must stop the request"))
    monkeypatch.setattr("app.integrations.lablup.time.monotonic", lambda: 100)
    client.deadline = 99
    with pytest.raises(LablupError, match="AI_TIMEOUT"):
        client.message(system="test", content=[], timeout_seconds=40)


def test_redirect_does_not_forward_claude_key():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://example.invalid"})

    with pytest.raises(LablupError, match="AI_UNAVAILABLE"):
        client_for(handler).message(system="test", content=[])
    assert len(calls) == 1


def test_partial_text_after_output_limit_is_not_accepted():
    with pytest.raises(LablupError, match="AI_OUTPUT_LIMIT"):
        client_for(lambda _: response('{"items":[]}', stop_reason="max_tokens")).message(
            system="test",
            content=[],
        )


def test_services_select_claude_and_use_provider_specific_vision_flags():
    settings = config(lablup_vision_enabled=False, claude_vision_enabled=True)
    for service in (AiService, QuestionAI, PreQuestionAI):
        assert isinstance(service(settings).client, ClaudeClient)
    assert settings.ai_model_id == "claude-sonnet-5-5"
    assert settings.ai_vision_enabled


def test_claude_cart_keeps_item_prices_and_photo_pairing():
    from test_cart_layout import cart_fixture, descriptions

    image, lines = cart_fixture()
    ImageDraw.Draw(image).rectangle((140, 420, 340, 620), fill="red")
    ImageDraw.Draw(image).rectangle((140, 1030, 340, 1230), fill="blue")
    data = io.BytesIO()
    image.save(data, format="PNG")
    image.close()
    table = "left\ttop\twidth\theight\tconfidence\ttext\n" + "\n".join(
        f"{line.left}\t{line.top}\t{line.right - line.left}\t{line.bottom - line.top}\t95\t{line.text}"
        for line in lines
    )
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response(json.dumps(descriptions()))

    client = client_for(handler, claude_cart_mode="verified")
    result = AiService(
        client.settings,
        client,
        ocr=lambda *_: table,
        price_reader=lambda *_: ["27,500", "20,500"],
    ).extract_cart(data.getvalue())
    assert result.analysis_method == "vision"
    assert [item.price for item in result.items] == [27500, 20500]
    assert all(item.image_crop for item in result.items)
    assert len([b for b in calls[0]["messages"][0]["content"] if b["type"] == "image"]) == 2
    assert calls[0]["thinking"] == {"type": "between_tools"}
    assert client.deadline is None


def test_discount_uses_claude_factory_without_lablup_vision_flag(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return response('{"shipping_fee":3000}')

    client = client_for(handler)
    monkeypatch.setattr("app.services.discounts.create_ai_client", lambda _: client)
    result = extract_discount(png_bytes(), client.settings)
    assert result["analysis_method"] == "vision"
    assert result["conditions"]["shipping_fee"] == 3000
    assert calls[0].url.host == "api.anthropic.com"
    payload = json.loads(calls[0].content)
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert payload["max_tokens"] == 1024


def test_summary_uses_claude_factory(monkeypatch):
    request = LocalSummaryRequest.model_validate(
        {
            "responses": [
                {
                    "date": "2026-11-01",
                    "template_code": "EXPECTED_USE_DAYS",
                    "question": "며칠 쓸까요?",
                    "answer": {"number_value": 5},
                }
            ]
        }
    )
    client = client_for(
        lambda _: response('{"summary":"5일 사용을 예상했어요.","source_dates":["2026-11-01"]}')
    )
    monkeypatch.setattr("app.services.local_questions.create_ai_client", lambda _: client)
    result = summarize_local(request, {"response_summary": "fallback"}, client.settings)
    assert result["method"] == "AI"
    assert client.last_metrics["provider"] == "claude"


def test_live_probe_is_mockable_and_reports_only_safe_metrics():
    client = client_for(lambda _: response())
    status, report = probe(client.settings, text_only=True, client=client)
    assert status == 0 and report["text_verified"]
    assert report["requests"][0]["input_tokens"] == 50
    assert "secret" not in json.dumps(report)


def test_probe_missing_key_is_actionable_without_network():
    client = client_for(lambda _: pytest.fail("No key"), claude_api_key=None)
    status, report = probe(client.settings, client=client)
    assert status == 2
    assert report["error"] == "AI_NOT_CONFIGURED"


def test_claude_item_draft_keeps_user_name_and_price(monkeypatch):
    client = client_for(
        lambda _: response(
            json.dumps(
                {
                    "items": [
                        {
                            "name": "AI replacement",
                            "price": 99999,
                            "category": "DIGITAL",
                            "supplemental_question": "오늘 키보드가 필요한 순간이 있었나요?",
                        }
                    ]
                }
            )
        )
    )
    monkeypatch.setattr("app.services.ai.create_ai_client", lambda _: client)
    result = AiService(client.settings).draft_item("나의 키보드", 20000)
    assert result.analysis_method == "text"
    assert result.items[0].name == "나의 키보드"
    assert result.items[0].price == 20000
    assert client.last_metrics["provider"] == "claude"


def test_claude_question_api_persists_actual_model(app, client, guest_headers, today, session, monkeypatch):
    from app.api.pre_purchase import get_pre_question_ai
    from tests.test_core import create_item
    from app.services import swipe_questions as swipe

    item = create_item(client, guest_headers, mode="ADAPTIVE")
    today.value += timedelta(days=1)
    monkeypatch.setattr(swipe.secrets, "choice", lambda pool: next(row for row in pool if row.code == "SW_PRE_NEED"))
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][0]["content"][0]["text"])
        requests.append(data)
        return response(json.dumps({
            "template_code": data["selected_template"]["template_code"],
            "question_text": data["selected_template"]["question"],
        }))

    transport = client_for(handler)
    ai = PreQuestionAI(transport.settings, transport)
    app.dependency_overrides[get_pre_question_ai] = lambda: ai
    result = client.post(
        f"/api/v1/simulations/{item['simulation']['id']}/daily-question",
        headers=guest_headers,
    )
    assert result.status_code == 200, result.text
    question = session.scalar(select(SimulationQuestion))
    assert question.model_id == "claude-sonnet-5-5"
    assert question.template_code == "SW_PRE_NEED"
    assert requests[0]["selected_template"]["template_code"] == "SW_PRE_NEED"


@pytest.mark.parametrize("vision,status,reply", [
    (True, 200, '{"shipping_fee":3000}'),
    (True, 400, ""),
    (True, 200, "invalid JSON"),
    (False, 200, ""),
])
def test_claude_discount_never_calls_ocr_even_on_failure(monkeypatch, vision, status, reply):
    calls = []

    def no_ocr(*_):
        pytest.fail("Claude discount analysis must never run OCR")

    def handler(request):
        calls.append(request)
        return response(reply) if status == 200 else httpx.Response(
            status, json={"error": {"message": "This model does not support image input"}}
        )

    monkeypatch.setattr("app.services.discounts.extract_ocr", no_ocr)
    transport = client_for(handler, claude_vision_enabled=vision)
    result = extract_discount(png_bytes(), transport.settings, transport)
    assert len(calls) == (1 if vision else 0)
    assert result["analysis_method"] == ("vision" if vision and status == 200 and reply.startswith("{") else "manual")
