import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.integrations.lablup import LablupError
from app.services.pre_purchase_questions import PreQuestionAI
from app.services import swipe_questions as swipe


def context(**changes):
    return {"item_name": "요가매트", "category": "HOBBY", "purchase_reasons": ["NECESSITY"], **changes}


def chosen(code, ctx=None):
    return swipe.select_question([swipe.PRE_BY_CODE[code]], [], ctx or context())[0]


@pytest.mark.parametrize(
    "name,category,frequent",
    [
        ("요가매트", "HOBBY", True),
        ("키보드", "DIGITAL", True),
        ("치약", "BEAUTY", True),
        ("기념 피규어", "HOBBY", False),
        ("파티 드레스", "CLOTHING", False),
        ("여행용 요가매트", "HOBBY", False),
    ],
)
@pytest.mark.parametrize(
    "reasons,promo",
    [
        (["NECESSITY"], False),
        (["ADVERTISEMENT"], False),
        (["DISCOUNT_LIMITED"], True),
        (["EXTRA_DISCOUNT"], True),
    ],
)
def test_eligibility_before_random_selection(monkeypatch, name, category, frequent, reasons, promo):
    pools = []

    def pick(pool):
        pools.append({row.code for row in pool})
        return pool[-1]

    monkeypatch.setattr(swipe.secrets, "choice", pick)
    row = swipe.select_question(
        swipe.PRE_CATALOG, [], context(item_name=name, category=category, purchase_reasons=reasons)
    )[0]
    assert ("SW_PRE_TODAY" in pools[0]) == frequent
    assert ("SW_PRE_PROMO" in pools[0]) == promo
    assert "SW_PRE_CUSTOM" in pools[0]
    assert row.code == "SW_PRE_CUSTOM"  # Selection uses the injected random draw, not bank order.


def test_unseen_intents_first_recent_three_excluded_even_after_bank_exhausted(monkeypatch):
    monkeypatch.setattr(swipe.secrets, "choice", lambda pool: pool[-1])
    history, codes = [], []
    for day in range(24):
        row = swipe.select_question(swipe.PRE_CATALOG, history, context())[0]
        assert row.code not in codes[-3:]
        codes.append(row.code)
        history.append(SimpleNamespace(date=date(2026, 11, 1) + timedelta(days=day), template_code=row.code))
    assert len(set(codes[:8])) == 8
    assert len(set(codes[8:16])) == 8


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def message(self, **kwargs):
        self.calls.append(kwargs)
        answer = next(self.replies)
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer, ensure_ascii=False)


@pytest.mark.parametrize(
    "text",
    [
        "요가매트를 어디에서 사용할 것 같나요?",
        "요가매트 없이도 운동할 수 있으니 안 사도 되나요?",
        "지금 가진 요가매트로 대신 쓸 수 있나요?",
        "요가매트를 사용했더니 운동이 편해졌나요?",
        "요가매트를 구매한 뒤 미끄러움이 줄었나요?",
        "요가매트를 사용할 시간을 언제 낼 건가요?",
        "요가매트를 사면 좋겠나요?",
    ],
)
def test_open_ended_reversed_experienced_or_trivial_questions_are_rejected(text):
    template = chosen("SW_PRE_CUSTOM")
    client = FakeClient([{"template_code": template.code, "question_text": text}] * 2)
    code, wording, method, reason = swipe.adapt_question(client, template, context())
    assert method == "TEMPLATE" and reason == "AI_INVALID_QUESTION"
    assert code == template.code and wording == template.wording
    assert len(client.calls) == 2


def test_ai_custom_question_and_particle_normalization():
    template = chosen("SW_PRE_CUSTOM")
    client = FakeClient(
        [
            {
                "template_code": template.code,
                "question_text": "요가매트를 사면 매트를 펼칠 공간을 꾸준히 확보할 수 있나요?",
            }
        ]
    )
    ai = PreQuestionAI(Settings(_env_file=None, lablup_api_key=None), client)
    code, wording, method, _ = ai.adapt([template], context(reason_other_text="매일 스트레칭"))
    assert method == "AI" and code == template.code
    assert "요가매트을(를)" in wording
    payload = json.loads(client.calls[0]["content"][0]["text"])
    assert payload["context"]["reason_other_text"] == "매일 스트레칭"
    assert payload["selected_template"]["original"] == template.original
    assert client.calls[0]["thinking"] is False


@pytest.mark.parametrize(
    "code,text",
    [
        ("SW_PRE_ALTERNATIVE", "기존 요가매트을(를) 이미 가지고 있나요?"),
        ("SW_PRE_CASH", "요가매트 대신 현금으로 갖고 있을 건가요?"),
        ("SW_PRE_PROMO", "할인 때문에 요가매트을(를) 서둘러 살 건가요?"),
        ("SW_PRE_TODAY", "요가매트을(를) 오늘 활용할 계획이 있나요?"),
    ],
)
def test_fixed_intents_cannot_be_reversed_or_changed(code, text):
    ctx = context(purchase_reasons=["DISCOUNT_LIMITED"])
    assert not swipe.valid_wording(text, chosen(code, ctx), ctx)


def test_ai_cannot_change_selected_intent_and_timeout_uses_product_fallback():
    template = chosen("SW_PRE_NEED")
    client = FakeClient(
        [
            {"template_code": "SW_PRE_CUSTOM", "question_text": template.wording},
            {"template_code": template.code, "question_text": template.wording},
        ]
    )
    assert swipe.adapt_question(client, template, context())[2] == "AI"
    assert len(client.calls) == 2
    fallback = swipe.adapt_question(FakeClient([LablupError("AI_TIMEOUT")]), template, context())
    assert fallback[2:] == ("TEMPLATE", "AI_TIMEOUT")
    assert "요가매트" in fallback[1] and "불편" in fallback[1]


@pytest.mark.parametrize("code", list(swipe.PRE_BY_CODE))
def test_all_fixed_fallbacks_keep_their_intent_and_accept_a_closed_ai_echo(code):
    ctx = context(purchase_reasons=["DISCOUNT_LIMITED"])
    template = chosen(code, ctx)
    assert swipe.valid_wording(template.wording, template, ctx)
    assert template.answer_spec == swipe.ANSWER_SPEC
    assert not any(word in template.wording for word in ("며칠", "어디", "어떻게"))


def test_long_names_still_produce_valid_local_response_length():
    row = swipe.select_question(swipe.PRE_CATALOG, [], context(item_name="요가매트" * 40))[0]
    assert len(row.wording) <= 300
