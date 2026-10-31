# 구매 후 AI 맞춤 질문

> 신규 `LOCAL` 구매도 이 고정 문항을 사용하지만 질문·답변 원문을 기기에만 저장합니다. `/purchases/{id}/local-question`은 [handoff 연동 계약](handoff-integration.md)을 따릅니다. 아래 DB 저장 API는 이전 모드 호환용입니다.

사용자가 제공한 구매 후 질문 이미지의 15개 문항을 고정 목록으로 사용합니다. 구매 전 14개 문항은 [별도 계약](pre-purchase-questions.md)을 따릅니다. 구매 후에는 **구매 건별 하루 한 질문**을 AI가 적합한 후보에서 선택하고 품목에 맞게 바꿉니다.

질문 정의는 `app/services/question_catalog.py`의 `purchase-v1`입니다. 원본 질문, 질문 코드, 응답 의미는 서버가 관리합니다. AI는 허용된 코드와 질문 문장만 반환하며 선택지·기간·계산값을 만들지 않습니다. 원본 목록은 `GET /api/v1/question-templates`로 확인할 수 있습니다.

## 질문 후보와 시점

| 단계 | 고정 질문 코드 |
|---|---|
| 구매 당일~3일 후 | `EXPECTATION` 기대 비교, `MOOD` 기분, `CHAIN_PURCHASE` 연쇄 구매 욕구 |
| 4일 후부터 실제 사용 | `USAGE_DAYS` 사용일수, `USAGE_CONTEXT` 사용 상황, `NON_USE_REASON` 미사용 이유 |
| 한 달 후 가치 재평가 | `BUY_AGAIN` 다시 선택, `VALUE_WORTH` 가치, `GOAL_TRADEOFF` 목표와 비교 |
| 한 달 후 동기 회고 | `PROBLEM_SOLVED` 필요 해결, `TRUE_MOTIVE` 실제 구매 동기, `RECOMMEND` 추천 |
| 세 달 후 장기 사용 | `LONG_USAGE` 계속 사용, `REPURCHASE` 재구매, `RESALE_VALUE` 중고 예상가 |

한 달·세 달은 달력 기준이며 월말은 해당 월의 마지막 날로 맞춥니다. 한 달부터 세 달 전까지는 사용·가치·동기 질문, 세 달부터는 사용·장기 질문이 후보입니다. 매일 그중 한 문항을 생성합니다. 최근 두 문항을 가능한 한 반복하지 않습니다.

사용일수 질문은 **구매일부터 완전히 지난 최근 7일 구간**이 있을 때만 후보가 됩니다. 예: 11월 4일 구매 → 11월 11일에는 11월 4~10일, 11월 18일에는 11월 11~17일입니다. 같은 7일 구간은 한 번만 묻습니다. 이 방식으로 겹치는 주간 응답을 여러 번 더하지 않습니다. 놓친 과거 구간을 0일로 채우거나 미래 사용을 묻지 않습니다.

미사용 이유는 앞선 사용일수 응답이 0일일 때, 목표 비교는 활성 목표가 있을 때만 후보로 제공합니다. 사진의 사용 ‘횟수’는 사용자가 앞서 선택한 **사용일수**로 통일합니다. 기분·기대·추천·욕구 등은 사용일수로 환산하지 않습니다.

## Android 연결 순서

1. `GET /home` 또는 `GET /today`에서 `purchase_questions`를 읽습니다. 기존 `questions`는 구매 전 시뮬레이션입니다.
2. 목록의 `question`이 `null`이면 `POST /purchases/{purchase_id}/daily-question`을 호출합니다. 본문과 날짜 지정은 필요 없습니다.
3. 응답의 `question`, `reference_text`, `answer_spec`을 표시합니다. `reference_text`는 서버가 계산한 회고 날짜·금액·원래 구매 이유 등이며 `null`이면 생략합니다. 같은 구매·서울 날짜로 다시 호출하면 이미 저장된 질문을 반환합니다. 새 문장으로 재생성하지 않습니다.
4. `PUT /purchase-questions/{question_id}/answer`로 선택 또는 숫자를 저장합니다.
5. 완료 후 `/today`를 다시 읽습니다. 완료한 구매 질문은 `purchase_answered`로 이동합니다. `?include_answered=true`로 다시 볼 수 있습니다.
6. 과거 질문·답변은 `GET /purchases/{purchase_id}/questions?limit=30&offset=0`, 단일 질문은 `GET /purchase-questions/{id}`로 조회합니다.

질문 생성 POST는 `purchase_id + 서버의 서울 날짜`가 중복 방지 기준이므로 별도 `Idempotency-Key`를 요구하지 않습니다. 동시 요청에서도 DB에는 한 질문만 저장됩니다. 생성이 동시에 시작되면 모델 호출은 겹칠 수 있지만 먼저 저장된 문장이 최종 결과입니다.

`GET /today`나 푸시 워커에서 AI를 호출하지 않습니다. 질문 카드에 진입할 때 명시적으로 생성합니다. 워커는 구매 후 미응답 질문도 기존의 하루 한 건 묶음 알림에 포함합니다.

기존 `reviews`와 `/purchases/{id}/review`는 이전 Android의 14일 회고 호환 경로입니다. 새 구매 후 질문 화면은 `purchase_questions`를 사용하며 같은 사용자에게 두 흐름을 동시에 답하도록 요구하지 않습니다.

### 객관식

```json
{
  "template_code": "MOOD",
  "question": "운동화를 산 지금 기분은 어떤가요?",
  "answer_spec": {
    "type": "CHOICE",
    "options": [
      {"code": "EXCITED", "label": "설렘"},
      {"code": "NEUTRAL", "label": "덤덤함"},
      {"code": "REGRET", "label": "살짝 후회"}
    ]
  }
}
```

저장 요청:

```json
{"choice_code":"EXCITED"}
```

`USAGE_DAYS`는 `D0`~`D7` 및 `UNSURE` 선택지를 제공합니다. `context.usage_window.start/end` 날짜를 질문 옆에 표시합니다. 일일 질문이라고 사용일수 문항을 ‘오늘 몇 번’으로 표시하면 안 됩니다.

### 중고 예상가

`RESALE_VALUE`는 `answer_spec.type=INTEGER`, `unit=KRW`이며 입력 범위를 함께 반환합니다. 사용자가 예상 원화 금액을 입력하거나 ‘잘 모르겠어요’를 선택합니다.

```json
{"number_value":12000}
```

```json
{"choice_code":"UNSURE"}
```

선택값과 숫자값 중 정확히 하나만 보냅니다. 해당 질문에 없는 선택지, 소수·음수·불리언 숫자는 거부합니다. 중고 예상가와 구매가의 차이는 사용자 추정이며 실제 시세·실제 손실이나 지갑 지출로 기록하지 않습니다. 예상 중고가가 구매가보다 높으면 차이는 음수일 수 있습니다.

## 저장과 비교

`purchase_questions`에는 소유 게스트·구매 ID·서울 날짜, 원본 코드·버전, 실제 질문 문장, 선택지/숫자 범위, 생성 당시 품목·가격·이유·목표·관찰 구간, AI 모델과 생성 방식이 저장됩니다. 원본 질문 문구도 `context_snapshot.template_original`로 보존합니다.

`purchase_question_answers`에는 질문 ID당 하나의 답변이 저장됩니다. 오늘 답변은 수정 가능하며 재시도나 수정으로 행 수가 늘지 않습니다. 지난 날짜의 질문은 조회만 가능합니다. 타 게스트의 질문·답변은 접근할 수 없습니다.

사용일수 답변은 저장 시 `comparison`에 같은 7일 기준 예상 사용일수와 실제 사용일수, 퍼센트포인트 차이를 반환합니다. 예상은 기존 시뮬레이션이 정상 종료되고 확정 응답이 70% 이상일 때만 계산합니다. 단위가 다른 감정 답변을 비교에 넣지 않습니다.

이력의 `usage_summary`는 서로 겹치지 않는 주간 응답만 합산합니다. `observed_days`는 실제로 숫자 응답이 있는 관찰 기간, `reported_used_days`는 사용했다고 답한 날 수입니다. 모름·미응답은 집계하지 않습니다. `cost_per_reported_used_day`는 기록된 기간의 사용일수로 나눈 구매 가격입니다. `covers_entire_elapsed_period=false`이면 이를 구매 이후 전체 기간의 비용이라고 표시하지 않습니다. 사용일수 0이면 비용은 `null`입니다.

가치 질문의 금액은 서버의 `reference_text`를 표시합니다. 근거는 `context.purchase_price`와 `context.usage_summary`이며 기록이 없으면 비용을 만들어 내지 않습니다. 구매 전 `FIRST_USE`에 답한 경우 `predicted_usage_context`로, 해결할 문제를 쓴 경우 `problem_to_solve`로 구매 당시 고정한 답변을 전달합니다. 없으면 `null`이며 과거 답변을 지어내지 않습니다. 실제 사용 응답 전에는 예상 사용일수·배수를 먼저 보여 주지 않고 응답 후 `comparison`으로 비교합니다. 사용 상황 문항은 실제 상황과 예측 상황의 일치 여부도 반환합니다.

리포트는 비교 가능한 주간 사용 답변을 집계합니다. 해당 조회 범위에서 같은 구매의 주간 답변이 있으면 이전 14일 회고보다 우선해 이중 집계를 막습니다. `sample_count`는 구매 건수, `measurement_count`는 집계한 관찰 구간 수입니다. 주간 숫자 응답은 실제 범위의 최솟값·최댓값이 같은 값이며, 이전 구간형 회고가 포함될 때만 `is_estimate=true`입니다.

## AI 실패와 배포

형식 오류·허용되지 않은 코드·사용일수를 횟수로 바꾼 문장은 한 번 보정 요청합니다. 다시 실패하거나 시간 초과·미설정이면 품목명이 포함된 고정 템플릿을 저장합니다. `generation_method=TEMPLATE`와 안전한 오류 코드만 반환하며 외부 응답이나 API 키는 노출하지 않습니다. 대체 질문도 당일에는 고정됩니다.

`alembic upgrade head`로 `0003_purchase_questions`를 적용한 후 API와 워커를 함께 재시작합니다. 기존 시뮬레이션·구매·회고는 유지합니다. 질문 이력이 있는 DB에서 `0003` 다운그레이드는 데이터를 버리지 않도록 거부하며 백업 복원이나 후속 마이그레이션을 사용합니다.

테스트는 Lablup 대역으로 선택·보정·시간 초과·저장과 재조회·동시 요청을 검증합니다. 실제 Lablup 모델의 질문 품질은 서버 API 키 설정 후 별도로 확인해야 합니다.
